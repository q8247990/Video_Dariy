"""SQLite unit tests for :class:`OutboxRepository`.

These tests stand up an in-memory SQLite engine and exercise the
:class:`OutboxRepository` API surface against the contract layer
from Todo 11. They cover:

- ``enroll_with_task_log`` atomic with a TaskLog row.
- ``enqueue_command`` thin wrapper.
- ``try_claim_one`` publisher hot path with a row that's immediately
  eligible (``next_attempt_at`` pinned to the past).
- ``mark_published`` / ``mark_failed_to_retry`` /
  ``mark_failed_terminal`` state-machine updates.
- ``cleanup_published_older_than`` retention helper.
- ``count_pending_older_than`` backlog helper.

PostgreSQL-specific concurrency tests (``FOR UPDATE SKIP LOCKED``,
concurrent ``INSERT … ON CONFLICT`` races, partial unique index,
audit / FK constraints) live in
``tests/integration/test_outbox_repository_postgres.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401  (registers the OutboxEvent model on Base.metadata)
from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxCommand, OutboxEvent
from src.application.outbox.enqueue import enqueue_command
from src.application.outbox.repository import EnrollOutcome, OutboxRepository
from src.application.outbox.state_machine import OutboxStatus
from src.db.base_class import Base
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    """Reset the in-process duplicate-event-id set.

    ``emit_event`` keeps a process-local ``_EMITTED_EVENT_IDS`` set as
    a fast early-reject; tests must clear it so prior test runs do not
    poison the next test's first emit.
    """
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture
def db_session() -> Session:
    """Return a function-scoped SQLite session bound to an in-memory engine.

    ``Base.metadata.create_all`` stands up the full schema (including
    the ``outbox_event`` table) so the unit tests exercise the same
    column / constraint contract as production — just without the
    partial indexes that PostgreSQL alone supports.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_task_log(db: Session) -> TaskLog:
    """Insert a minimal ``TaskLog`` and return it (already committed)."""
    task_log = TaskLog(task_type="outbox.unit", status="pending")
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _make_command(task_name: str = "src.tasks.analyzer.analyze_session_task") -> OutboxCommand:
    """Build a deterministic :class:`OutboxCommand` for tests."""
    return OutboxCommand(
        task_name=task_name,
        queue="analysis_hot",
        args=(42,),
        kwargs={"priority": "hot"},
    )


def _past(dt: datetime, seconds: int = 60) -> datetime:
    """Return ``dt - seconds`` for ``next_attempt_at`` pinning."""
    return dt - timedelta(seconds=seconds)


def _pin_next_attempt_to_past(db: Session, task_log_id: int) -> None:
    """Force the row's ``next_attempt_at`` to a past timestamp.

    The contract layer sets ``next_attempt_at = created_at`` which is
    "now"; the publisher claim path uses
    ``WHERE next_attempt_at <= now``, which never matches a row whose
    timestamp was just emitted. Tests that want to claim right away
    pin the timestamp so the claim path is exercised.
    """
    pinned = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    db.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_id).update(
        {"next_attempt_at": pinned}
    )
    db.commit()


# ---------------------------------------------------------------------------
# enroll_with_task_log
# ---------------------------------------------------------------------------


def test_enroll_creates_one_row_with_db_assigned_ids(db_session: Session) -> None:
    """A fresh enroll writes one row, returns ``created=True``, and
    backfills ``id`` / ``event_id`` so the dataclass is consistent
    with the database."""
    task_log = _make_task_log(db_session)
    repo = OutboxRepository(db_session)
    command = _make_command()

    outcome = repo.enroll_with_task_log(task_log, command)

    assert isinstance(outcome, EnrollOutcome)
    assert outcome.created is True
    assert outcome.event.id is not None
    assert outcome.event.event_id is not None
    assert outcome.event.task_log_id == task_log.id
    assert outcome.event.status is OutboxStatus.PENDING
    assert outcome.event.task_name == command.task_name
    assert outcome.event.queue == command.queue
    assert outcome.event.args_json == [42]
    assert outcome.event.kwargs_json == {"priority": "hot"}

    # Persisted row exists.
    rows = db_session.query(OutboxEventRow).all()
    assert len(rows) == 1
    assert rows[0].id == outcome.event.id


def test_enroll_duplicate_task_log_returns_created_false(db_session: Session) -> None:
    """A second enroll for the same ``task_log_id`` returns the
    existing row with ``created=False`` thanks to ``INSERT … ON
    CONFLICT (task_log_id) DO NOTHING`` on the SQLite ``UNIQUE``
    constraint."""
    task_log = _make_task_log(db_session)
    repo = OutboxRepository(db_session)

    first = repo.enroll_with_task_log(task_log, _make_command())
    assert first.created is True

    second = repo.enroll_with_task_log(task_log, _make_command())
    assert second.created is False
    assert second.event.id == first.event.id
    assert second.event.event_id == first.event.event_id

    rows = db_session.query(OutboxEventRow).all()
    assert len(rows) == 1


def test_enroll_rejects_unknown_task_name(db_session: Session) -> None:
    """The contract layer rejects unknown ``task_name``; the row is not
    INSERTed and the caller receives :class:`OutboxRegistryError`."""
    from src.application.outbox.errors import OutboxRegistryError

    task_log = _make_task_log(db_session)
    repo = OutboxRepository(db_session)

    with pytest.raises(OutboxRegistryError):
        repo.enroll_with_task_log(task_log, _make_command("src.tasks.unknown.bad_task"))

    assert db_session.query(OutboxEventRow).count() == 0


def test_enroll_rejects_empty_task_log_id() -> None:
    """The repository raises ``ValueError`` when ``task_log.id`` is
    ``None`` so the caller knows to flush first."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        task_log = TaskLog(task_type="outbox.unit", status="pending")
        # Intentionally NOT flushed / committed; ``id`` is None.
        session.add(task_log)
        repo = OutboxRepository(session)

        with pytest.raises(ValueError, match="task_log.id"):
            repo.enroll_with_task_log(task_log, _make_command())
    finally:
        session.close()
        engine.dispose()


def test_enqueue_command_is_a_thin_wrapper(db_session: Session) -> None:
    """``enqueue_command`` delegates to the repository and produces
    the same row shape."""
    task_log = _make_task_log(db_session)

    outcome = enqueue_command(db_session, _make_command(), task_log)

    assert outcome.created is True
    assert outcome.event.task_log_id == task_log.id
    assert db_session.query(OutboxEventRow).count() == 1


# ---------------------------------------------------------------------------
# try_claim_one
# ---------------------------------------------------------------------------


def test_try_claim_one_flips_pending_to_publishing(db_session: Session) -> None:
    """A claimable row is moved to ``publishing`` with the supplied
    ``claimed_by`` and ``lease_expires_at``."""
    task_log = _make_task_log(db_session)
    repo = OutboxRepository(db_session)
    repo.enroll_with_task_log(task_log, _make_command())
    _pin_next_attempt_to_past(db_session, task_log.id)

    pinned_past = _past(datetime.now(tz=timezone.utc))
    claimed = repo.try_claim_one(claimed_by="worker-A", now=pinned_past)

    assert claimed is not None
    assert claimed.status is OutboxStatus.PUBLISHING
    assert claimed.claimed_by == "worker-A"
    assert claimed.lease_expires_at is not None
    # SQLite TIMESTAMP returns naive datetimes; compare on the wall-clock.
    assert claimed.lease_expires_at.replace(tzinfo=timezone.utc) >= pinned_past


def test_second_claim_one_sees_publishing_returns_none(db_session: Session) -> None:
    """Two consecutive claims against a one-row pool — the second
    must observe the row already in ``publishing`` and return
    ``None``."""
    task_log = _make_task_log(db_session)
    repo = OutboxRepository(db_session)
    repo.enroll_with_task_log(task_log, _make_command())
    _pin_next_attempt_to_past(db_session, task_log.id)

    pinned_past = _past(datetime.now(tz=timezone.utc))
    first = repo.try_claim_one(claimed_by="worker-A", now=pinned_past)
    second = repo.try_claim_one(claimed_by="worker-B", now=pinned_past)

    assert first is not None
    assert first.status is OutboxStatus.PUBLISHING
    assert second is None


def test_try_claim_one_returns_none_for_no_rows(db_session: Session) -> None:
    """An empty pool returns ``None``."""
    repo = OutboxRepository(db_session)
    assert repo.try_claim_one(claimed_by="worker-A") is None


# ---------------------------------------------------------------------------
# mark_published / mark_failed_to_retry / mark_failed_terminal
# ---------------------------------------------------------------------------


def _claim_pending_row(db_session: Session, repo: OutboxRepository) -> OutboxEvent:
    """Helper: enroll + claim one row so the row is in ``publishing``."""
    task_log = _make_task_log(db_session)
    repo.enroll_with_task_log(task_log, _make_command())
    _pin_next_attempt_to_past(db_session, task_log.id)
    pinned_past = _past(datetime.now(tz=timezone.utc))
    claimed = repo.try_claim_one(claimed_by="worker-A", now=pinned_past)
    assert claimed is not None
    return claimed


def test_mark_published_clears_lease_and_sets_published_at(
    db_session: Session,
) -> None:
    claimed = _claim_pending_row(db_session, OutboxRepository(db_session))
    repo = OutboxRepository(db_session)

    fixed_now = datetime.now(tz=timezone.utc)
    published = repo.mark_published(claimed.event_id, now=fixed_now)

    assert published is not None
    assert published.status is OutboxStatus.PUBLISHED
    # SQLite returns naive datetimes; re-attach UTC for the equality check.
    assert published.published_at is not None
    assert published.published_at.replace(tzinfo=timezone.utc) == fixed_now
    assert published.claimed_by is None
    assert published.lease_expires_at is None


def test_mark_published_is_conditional_on_publishing(db_session: Session) -> None:
    """Calling ``mark_published`` on a row that is still in
    ``pending`` returns ``None`` — the conditional ``WHERE`` clause
    matches nothing."""
    task_log = _make_task_log(db_session)
    repo = OutboxRepository(db_session)
    outcome = repo.enroll_with_task_log(task_log, _make_command())

    result = repo.mark_published(outcome.event.event_id)
    assert result is None


def test_mark_published_idempotent_under_double_call(db_session: Session) -> None:
    """A second ``mark_published`` call on the same ``event_id``
    returns ``None`` because the row is no longer in ``publishing``;
    the row stays ``published``."""
    claimed = _claim_pending_row(db_session, OutboxRepository(db_session))
    repo = OutboxRepository(db_session)

    first = repo.mark_published(claimed.event_id)
    second = repo.mark_published(claimed.event_id)
    assert first is not None
    assert first.status is OutboxStatus.PUBLISHED
    assert second is None

    rows = db_session.query(OutboxEventRow).all()
    assert len(rows) == 1
    assert rows[0].status == "published"


def test_mark_failed_to_retry_flips_publishing_to_pending(
    db_session: Session,
) -> None:
    """A retryable failure leaves the row claimable: ``status``
    flips back to ``pending``, ``attempt_count`` increments,
    ``last_error`` and ``next_attempt_at`` are populated."""
    claimed = _claim_pending_row(db_session, OutboxRepository(db_session))
    repo = OutboxRepository(db_session)

    next_attempt = datetime.now(tz=timezone.utc) + timedelta(seconds=10)
    result = repo.mark_failed_to_retry(
        claimed.event_id,
        next_attempt_at=next_attempt,
        last_error="broker unreachable",
    )

    assert result is not None
    assert result.status is OutboxStatus.PENDING
    assert result.attempt_count == 1
    assert result.last_error == "broker unreachable"
    # SQLite returns naive datetimes; re-attach UTC for the equality check.
    assert result.next_attempt_at.replace(tzinfo=timezone.utc) == next_attempt
    assert result.claimed_by is None
    assert result.lease_expires_at is None

    # Re-claim succeeds — the row is back in pending.
    claim_now = datetime.now(tz=timezone.utc) + timedelta(seconds=20)
    reclaimed = repo.try_claim_one(claimed_by="worker-B", now=claim_now)
    assert reclaimed is not None


def test_mark_failed_terminal_flips_publishing_to_failed(db_session: Session) -> None:
    """``mark_failed_terminal`` is the terminal path; the row is
    operationally terminal — no publisher claim can pick it up."""
    claimed = _claim_pending_row(db_session, OutboxRepository(db_session))
    repo = OutboxRepository(db_session)

    result = repo.mark_failed_terminal(claimed.event_id, last_error="max attempts exceeded")

    assert result is not None
    assert result.status is OutboxStatus.FAILED
    assert result.last_error == "max attempts exceeded"
    assert result.claimed_by is None
    assert result.lease_expires_at is None

    pinned_past = _past(datetime.now(tz=timezone.utc))
    assert repo.try_claim_one(claimed_by="worker-B", now=pinned_past) is None


def test_mark_failed_to_retry_returns_none_on_pending_row(
    db_session: Session,
) -> None:
    """``mark_failed_to_retry`` is conditional on the row being in
    ``publishing``; calling it on a ``pending`` row returns ``None``."""
    task_log = _make_task_log(db_session)
    repo = OutboxRepository(db_session)
    outcome = repo.enroll_with_task_log(task_log, _make_command())

    result = repo.mark_failed_to_retry(
        outcome.event.event_id,
        next_attempt_at=datetime.now(tz=timezone.utc),
        last_error="noop",
    )
    assert result is None


# ---------------------------------------------------------------------------
# cleanup_published_older_than / count_pending_older_than
# ---------------------------------------------------------------------------


def _publish_one(
    db_session: Session,
    *,
    claimer: str = "worker-A",
    published_at: Optional[datetime] = None,
) -> OutboxEvent:
    """Helper: enroll → claim → publish a row in a single session."""
    task_log = _make_task_log(db_session)
    repo = OutboxRepository(db_session)
    repo.enroll_with_task_log(task_log, _make_command())
    _pin_next_attempt_to_past(db_session, task_log.id)
    pinned_past = _past(datetime.now(tz=timezone.utc))
    claimed = repo.try_claim_one(claimed_by=claimer, now=pinned_past)
    assert claimed is not None
    fixed = published_at or datetime.now(tz=timezone.utc)
    published = repo.mark_published(claimed.event_id, now=fixed)
    assert published is not None
    return published


def test_cleanup_published_older_than_deletes_only_old_rows(
    db_session: Session,
) -> None:
    """Only ``published`` rows older than the threshold are deleted;
    fresh rows are kept."""
    repo = OutboxRepository(db_session)
    old = _publish_one(db_session, published_at=datetime.now(tz=timezone.utc) - timedelta(days=40))
    fresh = _publish_one(db_session, published_at=datetime.now(tz=timezone.utc))

    threshold = datetime.now(tz=timezone.utc) - timedelta(days=30)
    deleted = repo.cleanup_published_older_than(threshold)

    assert deleted == 1
    remaining = db_session.query(OutboxEventRow).all()
    assert [row.id for row in remaining] == [fresh.id]
    assert old.id not in {row.id for row in remaining}


def test_count_pending_older_than_counts_publishing_with_expired_lease(
    db_session: Session,
) -> None:
    """A publishing row whose lease has expired still counts against
    the backlog — the publisher does not skip rows, only logs."""
    repo = OutboxRepository(db_session)

    # Pending row with next_attempt_at in the distant past.
    task_log_a = _make_task_log(db_session)
    repo.enroll_with_task_log(task_log_a, _make_command())
    db_session.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_a.id).update(
        {"next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=600)}
    )
    db_session.commit()

    # Publishing row whose lease has expired.
    task_log_b = _make_task_log(db_session)
    repo.enroll_with_task_log(task_log_b, _make_command())
    db_session.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_b.id).update(
        {
            "status": OutboxStatus.PUBLISHING.value,
            "claimed_by": "worker-A",
            "lease_expires_at": datetime.now(tz=timezone.utc) - timedelta(seconds=600),
        }
    )
    db_session.commit()

    count = repo.count_pending_older_than(seconds=300)
    assert count == 2


def test_count_pending_older_than_ignores_fresh_pending_rows(
    db_session: Session,
) -> None:
    """A pending row whose ``next_attempt_at`` is in the future does
    NOT count against the backlog (the publisher has not retried it
    yet)."""
    repo = OutboxRepository(db_session)

    task_log = _make_task_log(db_session)
    repo.enroll_with_task_log(task_log, _make_command())

    count = repo.count_pending_older_than(seconds=300)
    assert count == 0
