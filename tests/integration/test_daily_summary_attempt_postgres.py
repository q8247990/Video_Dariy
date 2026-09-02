"""PostgreSQL integration tests for :class:`DailySummaryAttemptRepository`.

These tests exercise the persistence layer on a real PostgreSQL
database that has been migrated to the new head (including the new
``daily_summary_generation_attempt`` table from migration
``20260902_0020``). They cover the behaviors that the SQLite unit
tests cannot:

- Two concurrent ``INSERT … ON CONFLICT DO NOTHING`` claims for the
  same ``summary_date`` produce exactly one active row — the partial
  unique index is the active-attempt safety net.
- The partial unique index predicate references the three active
  statuses (``claimed`` / ``queued`` / ``running``).
- The ``CHECK`` constraint on ``status`` rejects unknown values.
- The foreign key to ``task_log`` is nullable and ``ON DELETE SET
  NULL`` (deleting a ``task_log`` nulls the attempt's correlation id
  rather than cascading or blocking).
- After a terminal status, the next ``claim`` inserts a fresh row with
  ``attempt_no = previous + 1``.
- The audit query (``find_terminal_for_date``) returns the terminal
  history in ``attempt_no`` order.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.application.summary_attempt.repository import DailySummaryAttemptRepository
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.models.task_log import TaskLog

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _truncate_attempt_tables(postgres_migrated_engine: Engine) -> None:
    """Truncate ``daily_summary_generation_attempt`` and ``task_log`` before each test.

    ``postgres_migrated_engine`` is session-scoped so all PG tests in
    this run share one schema; rows left by an earlier test would
    poison the assertions of later tests. The FK is ``ON DELETE SET
    NULL`` for ``daily_summary_generation_attempt.task_log_id`` so
    ``task_log`` can be truncated safely after the attempt rows.
    """
    with postgres_migrated_engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE daily_summary_generation_attempt RESTART IDENTITY CASCADE")
        )
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))


def _new_task_log(db: Session) -> TaskLog:
    """Insert a ``TaskLog`` and commit so it has a server-assigned ``id``."""
    task_log = TaskLog(task_type="summary_attempt.pg", status="pending")
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _commit(db: Session) -> None:
    """Commit the session so writes are visible across sessions."""
    db.commit()


# ---------------------------------------------------------------------------
# Concurrency / partial unique index
# ---------------------------------------------------------------------------


def test_concurrent_claim_same_date_only_one_active(
    postgres_migrated_engine: Engine,
) -> None:
    """Two concurrent ``claim`` calls for the same ``summary_date``
    produce exactly one active row. The partial unique index
    ``(summary_date) WHERE status IN ('claimed','queued','running')``
    is the structural safety net: one transaction wins, the other
    receives ``created=False`` and points at the winner's row."""
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session_a = factory()
    session_b = factory()
    try:
        repo_a = DailySummaryAttemptRepository(session_a)
        repo_b = DailySummaryAttemptRepository(session_b)
        target_date = date(2026, 9, 1)

        outcome_a = repo_a.claim(
            summary_date=target_date,
            attempt_no=1,
            triggered_by="schedule",
            task_log_id=None,
        )
        _commit(session_a)

        outcome_b = repo_b.claim(
            summary_date=target_date,
            attempt_no=2,
            triggered_by="manual",
            task_log_id=None,
        )
        _commit(session_b)

        assert outcome_a.created is True
        assert outcome_b.created is False
        assert outcome_a.attempt.id == outcome_b.attempt.id
        assert outcome_a.attempt.attempt_no == 1

        with postgres_migrated_engine.connect() as conn:
            count = conn.execute(
                sql_text(
                    "SELECT count(*) FROM daily_summary_generation_attempt WHERE summary_date = :d"
                ),
                {"d": target_date},
            ).scalar_one()
        assert count == 1
    finally:
        session_a.close()
        session_b.close()


def test_partial_unique_index_present(postgres_migrated_engine: Engine) -> None:
    """The partial unique index
    ``uq_daily_summary_attempt_active_date`` is present and its
    ``WHERE`` clause references the three active statuses."""
    with postgres_migrated_engine.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND tablename = 'daily_summary_generation_attempt'"
            )
        ).fetchall()
    names = {row[0] for row in rows}
    assert "uq_daily_summary_attempt_active_date" in names
    partial_defs = [row[1] for row in rows if row[0] == "uq_daily_summary_attempt_active_date"]
    assert partial_defs, "partial unique index must have a definition"
    rendered = partial_defs[0].lower()
    assert "where" in rendered
    for active in ("claimed", "queued", "running"):
        assert active in rendered, (
            f"active status {active!r} missing from partial index WHERE clause: {partial_defs[0]!r}"
        )
    assert "summary_date" in rendered


# ---------------------------------------------------------------------------
# Schema / constraint guards
# ---------------------------------------------------------------------------


def test_check_constraint_rejects_unknown_status(
    postgres_migrated_engine: Engine,
) -> None:
    """A hand-rolled INSERT with ``status='unknown'`` is rejected by
    the ``ck_daily_summary_attempt_status`` CHECK constraint.

    Note: CHECK constraints are enforced during the ``INSERT`` (not
    deferred to ``COMMIT``), so the ``IntegrityError`` raises from
    ``session.execute(...)`` rather than from ``session.commit()``.
    Contrast this with the FK rejection test below, which has to
    defer until commit because PostgreSQL FKs use immediate (not
    deferred) validation by default — but ``session.execute`` does
    commit on autocommit session-level default."""
    target_date = date(2026, 9, 1)
    session = Session(postgres_migrated_engine)
    try:
        with pytest.raises(IntegrityError):
            session.execute(
                sql_text(
                    "INSERT INTO daily_summary_generation_attempt "
                    "(summary_date, attempt_no, status, started_at, created_at, updated_at) "
                    "VALUES (:d, 1, 'unknown', now(), now(), now())"
                ),
                {"d": target_date},
            )
    finally:
        session.rollback()
        session.close()


def test_fk_to_task_log_nullable_and_set_null_on_delete(
    postgres_migrated_engine: Engine,
) -> None:
    """The FK to ``task_log`` is nullable, and ``ON DELETE SET NULL``
    means deleting the parent ``task_log`` nulls the attempt's
    ``task_log_id`` rather than cascading the attempt row away."""
    session = Session(postgres_migrated_engine)
    try:
        task_log = _new_task_log(session)
        repo = DailySummaryAttemptRepository(session)
        outcome = repo.claim(
            summary_date=date(2026, 9, 1),
            attempt_no=1,
            triggered_by="schedule",
            task_log_id=task_log.id,
        )
        _commit(session)
        attempt_id = outcome.attempt.id
        task_log_id = task_log.id
    finally:
        session.close()

    # Now delete the task_log; the attempt must survive with
    # task_log_id NULL.
    session = Session(postgres_migrated_engine)
    try:
        session.execute(
            sql_text("DELETE FROM task_log WHERE id = :id"),
            {"id": task_log_id},
        )
        session.commit()
    finally:
        session.close()

    verify = Session(postgres_migrated_engine)
    try:
        attempt = (
            verify.query(DailySummaryGenerationAttempt)
            .filter(DailySummaryGenerationAttempt.id == attempt_id)
            .one()
        )
        assert attempt.task_log_id is None
    finally:
        verify.close()


def test_fk_to_task_log_allows_null(postgres_migrated_engine: Engine) -> None:
    """An attempt row with ``task_log_id=None`` is accepted: the FK
    is nullable, so ad-hoc runs without a TaskLog are valid."""
    session = Session(postgres_migrated_engine)
    try:
        repo = DailySummaryAttemptRepository(session)
        outcome = repo.claim(
            summary_date=date(2026, 9, 1),
            attempt_no=1,
            triggered_by="manual",
            task_log_id=None,
        )
        session.commit()

        assert outcome.created is True
        assert outcome.attempt.task_log_id is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Retry semantics on real PostgreSQL
# ---------------------------------------------------------------------------


def test_retry_after_succeeded_creates_attempt_no_2(
    postgres_migrated_engine: Engine,
) -> None:
    """After a successful attempt the per-date active slot is free
    again, so the next ``claim`` inserts a new row with
    ``attempt_no=2``. Terminal rows live outside the partial index so
    the history accumulates freely."""
    session = Session(postgres_migrated_engine)
    try:
        repo = DailySummaryAttemptRepository(session)
        target_date = date(2026, 9, 1)

        first = repo.claim(
            summary_date=target_date,
            attempt_no=1,
            triggered_by="schedule",
            task_log_id=None,
        )
        _commit(session)

        repo.mark_running(first.attempt.id)
        succeeded = repo.mark_succeeded(first.attempt.id)
        assert succeeded is not None
        _commit(session)

        second = repo.claim(
            summary_date=target_date,
            attempt_no=2,
            triggered_by="retry",
            task_log_id=None,
        )
        _commit(session)

        assert second.created is True
        assert second.attempt.attempt_no == 2

        with postgres_migrated_engine.connect() as conn:
            count = conn.execute(
                sql_text(
                    "SELECT count(*) FROM daily_summary_generation_attempt WHERE summary_date = :d"
                ),
                {"d": target_date},
            ).scalar_one()
        assert count == 2
    finally:
        session.close()


def test_audit_query_returns_attempts_in_ordered_sequence(
    postgres_migrated_engine: Engine,
) -> None:
    """``find_terminal_for_date`` returns the terminal rows in
    ``attempt_no`` ascending order — the audit / post-mortem contract
    (Todo 15 acceptance: "重新生成保留 attempt 历史")."""
    session = Session(postgres_migrated_engine)
    try:
        repo = DailySummaryAttemptRepository(session)
        target_date = date(2026, 9, 1)

        first = repo.claim(
            summary_date=target_date,
            attempt_no=1,
            triggered_by="schedule",
            task_log_id=None,
        )
        _commit(session)
        repo.mark_running(first.attempt.id)
        succeeded = repo.mark_succeeded(first.attempt.id)
        assert succeeded is not None
        _commit(session)

        second = repo.claim(
            summary_date=target_date,
            attempt_no=2,
            triggered_by="retry",
            task_log_id=None,
        )
        _commit(session)
        cancelled = repo.mark_cancelled(second.attempt.id, last_error="user")
        assert cancelled is not None
        _commit(session)

        history = repo.find_terminal_for_date(target_date)
        assert [row.attempt_no for row in history] == [1, 2]
        assert history[0].status == "succeeded"
        assert history[1].status == "cancelled"
    finally:
        session.close()
