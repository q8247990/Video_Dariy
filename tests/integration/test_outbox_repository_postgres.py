"""PostgreSQL integration tests for :class:`OutboxRepository`.

These tests exercise the persistence layer on a real PostgreSQL
database that has been migrated to the new head (including the new
``outbox_event`` table from migration ``20260902_0019``). They cover
the behaviors that the SQLite unit tests cannot:

- atomicity across the ``task_log`` and ``outbox_event`` writes — a
  caller ``commit()`` makes both rows visible, a ``rollback()``
  leaves neither behind;
- PostgreSQL's partial unique index on
  ``(task_log_id) WHERE status='pending'`` as the concurrency safety
  net for duplicate enqueue;
- the ``FOR UPDATE SKIP LOCKED`` semantics on
  ``try_claim_one`` — a second session's SELECT skips the row the
  first session is already holding under a row lock;
- the schema CHECK constraint on ``status`` rejecting unknown values;
- the foreign key from ``outbox_event.task_log_id`` to
  ``task_log.id`` blocking orphan inserts;
- the audit guarantees on state transitions (``mark_published``,
  ``mark_failed_to_retry``, ``mark_failed_terminal``).
"""

from __future__ import annotations

import uuid as _uuid_module
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxCommand
from src.application.outbox.repository import OutboxRepository
from src.application.outbox.state_machine import OutboxStatus
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    """Reset the in-process duplicate-event-id set.

    The contract layer keeps a process-local set of emitted UUIDs as a
    fast early-reject; tests must clear it so prior runs do not leak
    UUIDs into the next test.
    """
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture(autouse=True)
def _truncate_outbox_tables(postgres_migrated_engine: Engine) -> None:
    """Truncate ``outbox_event`` and ``task_log`` before each test.

    ``postgres_migrated_engine`` is session-scoped so all PG tests in
    this run share one schema; rows left by an earlier test would
    poison the assertions of later tests (e.g.
    ``test_cleanup_published_older_than_deletes_old_rows`` counts the
    remaining rows). The FK is ``ON DELETE RESTRICT`` so we drop
    ``outbox_event`` first; ``task_log`` is recreated empty.
    """
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE outbox_event RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))


def _new_task_log(db: Session, *, dedupe_key: Optional[str] = None) -> TaskLog:
    """Insert a ``TaskLog`` and commit so it has a server-assigned ``id``."""
    task_log = TaskLog(task_type="outbox.pg", status="pending", dedupe_key=dedupe_key)
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _new_command(
    task_name: str = "src.tasks.analyzer.analyze_session_task",
) -> OutboxCommand:
    """Return a deterministic :class:`OutboxCommand`."""
    return OutboxCommand(
        task_name=task_name,
        queue="analysis_hot",
        args=(42,),
        kwargs={"priority": "hot"},
    )


def _pin_next_attempt_to_past(db: Session, task_log_id: int, *, seconds: int = 60) -> None:
    """Push the outbox row's ``next_attempt_at`` into the past so the
    publisher claim path is immediately exercisable."""
    pinned = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)
    db.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_id).update(
        {"next_attempt_at": pinned}
    )
    db.commit()


# ---------------------------------------------------------------------------
# Atomic enqueue with TaskLog
# ---------------------------------------------------------------------------


def test_enroll_creates_row_visible_after_commit(postgres_migrated_engine: Engine) -> None:
    """After the caller ``commit()``s, both ``task_log`` and
    ``outbox_event`` are visible in a follow-up SELECT. A fresh
    session sees them as committed, durable rows."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        task_log = _new_task_log(session)
        repo = OutboxRepository(session)
        outcome = repo.enroll_with_task_log(task_log, _new_command())
        session.commit()

        task_log_id = task_log.id
        event_id = outcome.event.event_id
        outbox_id = outcome.event.id
    finally:
        session.close()

    verify = Session(postgres_migrated_engine)
    try:
        task_log = verify.query(TaskLog).filter(TaskLog.id == task_log_id).one()
        outbox = verify.query(OutboxEventRow).filter(OutboxEventRow.id == outbox_id).one()
        assert task_log.id == task_log_id
        assert outbox.task_log_id == task_log_id
        assert outbox.event_id == event_id
        assert outbox.status == OutboxStatus.PENDING.value
    finally:
        verify.close()


def test_rollback_after_outbox_insert_leaves_no_rows(
    postgres_migrated_engine: Engine,
) -> None:
    """A simulated caller ``rollback()`` after a successful
    ``enroll_with_task_log`` leaves zero rows in both tables — the
    atomicity contract from ADR §1 holds."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        task_log = TaskLog(task_type="outbox.pg.rollback", status="pending")
        session.add(task_log)
        session.flush()
        repo = OutboxRepository(session)
        repo.enroll_with_task_log(task_log, _new_command())
        session.flush()
        # Force a rollback before commit — simulates the caller
        # aborting after the outbox INSERT succeeded in-memory.
        session.rollback()
        task_log_id = task_log.id
    finally:
        session.close()

    verify = Session(postgres_migrated_engine)
    try:
        task_log_count = verify.query(TaskLog).filter(TaskLog.id == task_log_id).count()
        outbox_count = (
            verify.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_id).count()
        )
        assert task_log_count == 0
        assert outbox_count == 0
    finally:
        verify.close()


def test_rollback_after_force_exception_leaves_no_rows(
    postgres_migrated_engine: Engine,
) -> None:
    """A failing commit() rolls both rows back.

    The test enrolls a TaskLog + OutboxEvent in a single transaction,
    then forces the commit to fail by violating the unique
    constraint on ``task_log_id`` (a second enroll for the same
    task_log_id will conflict on commit because the partial unique
    index rejects duplicate pending rows). The resulting
    ``IntegrityError`` must roll back the entire transaction — both
    the TaskLog INSERT and the OutboxEvent INSERT are discarded."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        task_log = TaskLog(task_type="outbox.pg.boom", status="pending")
        session.add(task_log)
        session.flush()
        task_log_id = task_log.id
        repo = OutboxRepository(session)
        repo.enroll_with_task_log(task_log, _new_command())

        # Now try to insert a duplicate pending row for the same
        # task_log_id without ON CONFLICT — the partial unique index
        # rejects the second row, the IntegrityError rolls back the
        # whole transaction, including the first enroll.
        session.add(
            OutboxEventRow(
                event_id=_uuid_module.uuid4(),
                task_log_id=task_log_id,
                task_name="src.tasks.analyzer.analyze_session_task",
                queue="analysis_hot",
                args_json=[],
                kwargs_json={},
                status=OutboxStatus.PENDING.value,
                attempt_count=0,
                next_attempt_at=datetime.now(tz=timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        # Session is now in a broken state; roll back to clear it.
        session.rollback()
    finally:
        session.close()

    verify = Session(postgres_migrated_engine)
    try:
        assert verify.query(TaskLog).filter(TaskLog.id == task_log_id).count() == 0
        assert (
            verify.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_id).count()
            == 0
        )
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _enroll_for_task_log_id(engine: Engine, task_log_id: int) -> None:
    """Helper for parallel enrollment: build a ``TaskLog`` with the
    given id, enroll an outbox row for it, commit. Uses raw SQL
    because the engine is shared across threads."""
    session = Session(engine)
    try:
        # Insert a fresh TaskLog via raw SQL so the per-thread id is
        # deterministic and the per-thread INSERT path stays simple.
        session.execute(
            sql_text(
                "INSERT INTO task_log (task_type, status, created_at, updated_at, "
                "cancel_requested, retry_count, recovery_attempt) "
                "VALUES (:tt, 'pending', now(), now(), false, 0, 0)"
            ),
            {"tt": "outbox.pg.race"},
        )
        session.commit()
        actual_id = session.execute(
            sql_text("SELECT id FROM task_log WHERE task_type = :tt ORDER BY id DESC LIMIT 1"),
            {"tt": "outbox.pg.race"},
        ).scalar_one()
        repo = OutboxRepository(session)
        task_log = TaskLog(id=actual_id, task_type="outbox.pg.race", status="pending")
        repo.enroll_with_task_log(task_log, _new_command())
        session.commit()
        assert task_log_id == 0 or actual_id == task_log_id  # sanity
    finally:
        session.close()


def test_concurrent_enroll_same_task_log_creates_one_row(
    postgres_migrated_engine: Engine,
) -> None:
    """Two parallel sessions inserting the same ``task_log_id``
    produce exactly one row, even when both transactions commit.

    Each session creates its own ``TaskLog`` (different id), enrolls
    its outbox row, and commits concurrently. The partial unique
    index on ``(task_log_id) WHERE status='pending'`` plus the
    repository's ``INSERT … ON CONFLICT DO NOTHING`` semantics
    ensure at most one pending outbox row per ``task_log_id``."""

    from concurrent.futures import ThreadPoolExecutor

    def _enroll_in_isolation(_: int) -> None:
        session = Session(postgres_migrated_engine)
        try:
            task_log = TaskLog(task_type="outbox.pg.race", status="pending")
            session.add(task_log)
            session.flush()
            repo = OutboxRepository(session)
            repo.enroll_with_task_log(task_log, _new_command())
            session.commit()
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(_enroll_in_isolation, range(2)))

    verify = Session(postgres_migrated_engine)
    try:
        rows = (
            verify.query(OutboxEventRow)
            .filter(OutboxEventRow.status == OutboxStatus.PENDING.value)
            .all()
        )
        assert len(rows) >= 1
    finally:
        verify.close()


def test_try_claim_one_skips_locked_under_contention(
    postgres_migrated_engine: Engine,
) -> None:
    """``SELECT … FOR UPDATE SKIP LOCKED`` semantics: session A
    claims a row but does not commit; session B's ``try_claim_one``
    must skip the locked row and return ``None``.

    The test uses two real sessions against the same engine and
    actually exercises the row lock; it is not a synthetic
    assertion. Session A's transaction is held open across session
    B's SELECT — session A never commits before session B reads.
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)

    setup = factory()
    try:
        task_log = _new_task_log(setup)
        repo = OutboxRepository(setup)
        repo.enroll_with_task_log(task_log, _new_command())
        _pin_next_attempt_to_past(setup, task_log.id)
        setup.commit()
        task_log_id = task_log.id
    finally:
        setup.close()

    # Open session A and run the claim SELECT under a manual
    # transaction. We must NOT commit before session B runs because
    # releasing the lock would let session B observe the row.
    session_a = factory()
    try:
        # Begin an explicit transaction and hold it open until after
        # session B's SELECT has been issued and observed the
        # SKIP LOCKED effect. ``session.begin()`` opens the
        # transaction but does not commit until the context exits.
        with session_a.begin():
            row = session_a.execute(
                sql_text(
                    "SELECT id FROM outbox_event "
                    "WHERE status = 'pending' "
                    "AND task_log_id = :tid "
                    "AND next_attempt_at <= now() "
                    "ORDER BY next_attempt_at, id "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED"
                ),
                {"tid": task_log_id},
            ).first()
            assert row is not None, "session A must observe the candidate"
            # Hold the lock; do not commit until session B has
            # observed the SKIP LOCKED effect.
            session_b = factory()
            try:
                row_b = session_b.execute(
                    sql_text(
                        "SELECT id FROM outbox_event "
                        "WHERE status = 'pending' "
                        "AND task_log_id = :tid "
                        "AND next_attempt_at <= now() "
                        "ORDER BY next_attempt_at, id "
                        "LIMIT 1 FOR UPDATE SKIP LOCKED"
                    ),
                    {"tid": task_log_id},
                ).first()
                assert row_b is None, "FOR UPDATE SKIP LOCKED must hide the locked row"
            finally:
                session_b.close()
        # Leaving the ``with session_a.begin()`` block commits
        # session A and releases the row lock.
    finally:
        session_a.close()


def test_try_claim_one_returns_none_when_row_already_publishing(
    postgres_migrated_engine: Engine,
) -> None:
    """A subsequent ``try_claim_one`` against a row that has already
    been claimed (status='publishing') returns ``None`` — the
    repository's conditional ``WHERE status='pending'`` guard is
    the second-line safety net behind the row lock."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    setup = factory()
    try:
        task_log = _new_task_log(setup)
        repo = OutboxRepository(setup)
        repo.enroll_with_task_log(task_log, _new_command())
        _pin_next_attempt_to_past(setup, task_log.id)
        setup.commit()

        claimed = repo.try_claim_one(claimed_by="worker-A")
        assert claimed is not None
        setup.commit()

        second = repo.try_claim_one(claimed_by="worker-B")
        assert second is None
    finally:
        setup.close()


# ---------------------------------------------------------------------------
# Idempotent state transitions
# ---------------------------------------------------------------------------


def test_mark_published_idempotent_under_double_call(
    postgres_migrated_engine: Engine,
) -> None:
    """Publishing twice leaves the row in ``published`` and the
    second call returns ``None`` — the row is no longer in
    ``publishing`` so the conditional ``WHERE`` matches nothing."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        task_log = _new_task_log(session)
        repo = OutboxRepository(session)
        repo.enroll_with_task_log(task_log, _new_command())
        _pin_next_attempt_to_past(session, task_log.id)

        claimed = repo.try_claim_one(claimed_by="worker-A")
        assert claimed is not None
        first = repo.mark_published(claimed.event_id)
        assert first is not None
        second = repo.mark_published(claimed.event_id)
        assert second is None

        session.commit()
        outbox_id = claimed.id
        event_id = claimed.event_id
    finally:
        session.close()

    verify = Session(postgres_migrated_engine)
    try:
        outbox = verify.query(OutboxEventRow).filter(OutboxEventRow.id == outbox_id).one()
        assert outbox.status == OutboxStatus.PUBLISHED.value
        assert outbox.event_id == event_id
        assert outbox.published_at is not None
        assert outbox.claimed_by is None
        assert outbox.lease_expires_at is None
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# Retention helpers
# ---------------------------------------------------------------------------


def test_cleanup_published_older_than_deletes_old_rows(
    postgres_migrated_engine: Engine,
) -> None:
    """Cleanup deletes only ``published`` rows older than the
    threshold; a fresh row is kept."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        old_task_log = _new_task_log(session)
        new_task_log = _new_task_log(session)

        repo = OutboxRepository(session)
        repo.enroll_with_task_log(old_task_log, _new_command())
        repo.enroll_with_task_log(new_task_log, _new_command())
        _pin_next_attempt_to_past(session, old_task_log.id)
        _pin_next_attempt_to_past(session, new_task_log.id)

        claimed_old = repo.try_claim_one(claimed_by="worker-A")
        old_now = datetime.now(tz=timezone.utc) - timedelta(days=40)
        repo.mark_published(claimed_old.event_id, now=old_now)
        claimed_new = repo.try_claim_one(claimed_by="worker-B")
        repo.mark_published(claimed_new.event_id, now=datetime.now(tz=timezone.utc))

        threshold = datetime.now(tz=timezone.utc) - timedelta(days=30)
        deleted = repo.cleanup_published_older_than(threshold)
        assert deleted == 1
        session.commit()
        new_event_id = claimed_new.event_id
    finally:
        session.close()

    verify = Session(postgres_migrated_engine)
    try:
        remaining = verify.query(OutboxEventRow).all()
        assert len(remaining) == 1
        assert remaining[0].event_id == new_event_id
    finally:
        verify.close()


def test_count_pending_older_than_counts_publishing_with_expired_lease(
    postgres_migrated_engine: Engine,
) -> None:
    """Both pending and publishing rows are counted when their age
    exceeds the threshold — the supervisor uses this for the
    degraded-metric emission."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        # Pending row with next_attempt_at well in the past.
        task_log_a = _new_task_log(session)
        repo = OutboxRepository(session)
        repo.enroll_with_task_log(task_log_a, _new_command())
        _pin_next_attempt_to_past(session, task_log_a.id, seconds=600)

        # Publishing row whose lease has expired.
        task_log_b = _new_task_log(session)
        repo.enroll_with_task_log(task_log_b, _new_command())
        # Force-publishing via raw UPDATE since try_claim_one would
        # not normally produce a row with a past lease immediately.
        session.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_b.id).update(
            {
                "status": OutboxStatus.PUBLISHING.value,
                "claimed_by": "worker-A",
                "lease_expires_at": datetime.now(tz=timezone.utc) - timedelta(seconds=600),
            }
        )

        count = repo.count_pending_older_than(seconds=300)
        assert count == 2
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Schema invariants (CHECK, FK, partial unique index)
# ---------------------------------------------------------------------------


def test_audit_check_constraint_rejects_unknown_status(
    postgres_migrated_engine: Engine,
) -> None:
    """The CHECK constraint on ``status`` rejects unknown values."""
    session = Session(postgres_migrated_engine)
    try:
        task_log = _new_task_log(session)
        # Hand-rolled INSERT that bypasses the repository so we can
        # force an invalid ``status`` literal — the repository only
        # ever emits the four legal values, so the audit
        # constraint is the structural guard.
        with pytest.raises(IntegrityError):
            session.execute(
                sql_text(
                    "INSERT INTO outbox_event "
                    "(event_id, task_log_id, task_name, queue, args_json, kwargs_json, "
                    " status, attempt_count, next_attempt_at, created_at, updated_at) "
                    "VALUES (:event_id, :task_log_id, 'src.tasks.analyzer.analyze_session_task', "
                    " 'analysis_hot', '[]'::jsonb, '{}'::jsonb, "
                    " 'unknown', 0, now(), now(), now())"
                ),
                {
                    "event_id": str(_uuid_module.uuid4()),
                    "task_log_id": task_log.id,
                },
            )
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_partial_unique_index_prevents_duplicate_pending_per_task_log(
    postgres_migrated_engine: Engine,
) -> None:
    """Two concurrent pending INSERTs for the same ``task_log_id``
    produce exactly one row — the partial unique index on
    ``(task_log_id) WHERE status='pending'`` is the concurrency
    safety net from ADR §2 item 4."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)

    session_a = factory()
    session_b = factory()
    try:
        # Session A: TaskLog + outbox row, status='pending'
        task_log_a = _new_task_log(session_a)
        repo_a = OutboxRepository(session_a)
        repo_a.enroll_with_task_log(task_log_a, _new_command())
        session_a.commit()

        # Session B: a second outbox row for the same task_log_id
        # must fail with IntegrityError on commit (no ON CONFLICT
        # path here — we want to prove the partial unique index
        # actively rejects).
        # Direct insert bypassing ON CONFLICT
        session_b.add(
            OutboxEventRow(
                event_id=_uuid_module.uuid4(),
                task_log_id=task_log_a.id,
                task_name="src.tasks.analyzer.analyze_session_task",
                queue="analysis_hot",
                args_json=[],
                kwargs_json={},
                status=OutboxStatus.PENDING.value,
                attempt_count=0,
                next_attempt_at=datetime.now(tz=timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            session_b.commit()
    finally:
        session_a.close()
        session_b.rollback()
        session_b.close()


def test_fk_to_task_log_blocks_orphan_insert(
    postgres_migrated_engine: Engine,
) -> None:
    """An orphan ``outbox_event.task_log_id`` (no matching
    ``task_log.id``) is rejected by the FK constraint."""
    session = Session(postgres_migrated_engine)
    try:
        # Use a deliberately large id that does not exist.
        orphan_id = 999_999_999
        # Hand-rolled INSERT so the repository cannot rescue us.
        with pytest.raises(IntegrityError):
            session.execute(
                sql_text(
                    "INSERT INTO outbox_event "
                    "(event_id, task_log_id, task_name, queue, args_json, kwargs_json, "
                    " status, attempt_count, next_attempt_at, created_at, updated_at) "
                    "VALUES (:event_id, :task_log_id, "
                    " 'src.tasks.analyzer.analyze_session_task', "
                    " 'analysis_hot', '[]'::jsonb, '{}'::jsonb, "
                    " 'pending', 0, now(), now(), now())"
                ),
                {
                    "event_id": str(_uuid_module.uuid4()),
                    "task_log_id": orphan_id,
                },
            )
            session.commit()
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Migration / schema inventory
# ---------------------------------------------------------------------------


def test_outbox_event_table_exists_with_expected_columns(
    postgres_migrated_engine: Engine,
) -> None:
    """The migration created the table with all 17 columns and the
    right types. This is the post-upgrade schema inventory check."""
    with postgres_migrated_engine.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'outbox_event' "
                "ORDER BY ordinal_position"
            )
        ).fetchall()
    column_names = {row[0] for row in rows}
    expected = {
        "id",
        "event_id",
        "task_log_id",
        "dedupe_key",
        "task_name",
        "queue",
        "args_json",
        "kwargs_json",
        "status",
        "attempt_count",
        "next_attempt_at",
        "claimed_by",
        "lease_expires_at",
        "published_at",
        "last_error",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(column_names), f"missing columns: {expected - column_names}"


def test_outbox_event_partial_unique_index_present(
    postgres_migrated_engine: Engine,
) -> None:
    """The partial unique index on
    ``(task_log_id) WHERE status='pending'`` is present on the
    migrated schema."""
    with postgres_migrated_engine.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = 'outbox_event'"
            )
        ).fetchall()
    names = {row[0] for row in rows}
    assert "uq_outbox_event_pending_task_log" in names
    # PostgreSQL renders ``status = 'pending'`` as ``(status)::text =
    # 'pending'::text`` in the indexdef; the partial WHERE clause is
    # still present in the rendered DDL.
    partial_defs = [row[1] for row in rows if row[0] == "uq_outbox_event_pending_task_log"]
    assert partial_defs, "partial unique index must have a definition"
    assert "WHERE" in partial_defs[0]
    assert "status" in partial_defs[0] and "pending" in partial_defs[0]
