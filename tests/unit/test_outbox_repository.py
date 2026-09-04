"""PostgreSQL unit tests for :class:`OutboxRepository`.

These tests run against the project-wide ``pg_db`` fixture (a
function-scoped session on a one-shot ``alembic upgrade head`` schema
rolled back after every test). They exercise the
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
from sqlalchemy.orm import Session

from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxCommand, OutboxEvent
from src.application.outbox.enqueue import enqueue_command
from src.application.outbox.repository import EnrollOutcome, OutboxRepository
from src.application.outbox.state_machine import OutboxStatus
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


def _make_task_log(db: Session) -> TaskLog:
    """Insert a minimal ``TaskLog`` and return it (already flushed so its ``id`` is set)."""
    task_log = TaskLog(task_type="outbox.unit", status="pending")
    db.add(task_log)
    db.flush()
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


def test_enroll_creates_one_row_with_db_assigned_ids(pg_db: Session) -> None:
    """A fresh enroll writes one row, returns ``created=True``, and
    backfills ``id`` / ``event_id`` so the dataclass is consistent
    with the database."""
    task_log = _make_task_log(pg_db)
    repo = OutboxRepository(pg_db)
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
    rows = pg_db.query(OutboxEventRow).all()
    assert len(rows) == 1
    assert rows[0].id == outcome.event.id


def test_enroll_duplicate_task_log_returns_created_false(pg_db: Session) -> None:
    """A second enroll for the same ``task_log_id`` returns the
    existing row with ``created=False`` thanks to ``INSERT … ON
    CONFLICT (task_log_id) DO NOTHING`` on the partial unique
    predicate."""
    task_log = _make_task_log(pg_db)
    repo = OutboxRepository(pg_db)

    first = repo.enroll_with_task_log(task_log, _make_command())
    assert first.created is True

    second = repo.enroll_with_task_log(task_log, _make_command())
    assert second.created is False
    assert second.event.id == first.event.id
    assert second.event.event_id == first.event.event_id

    rows = pg_db.query(OutboxEventRow).all()
    assert len(rows) == 1


def test_enroll_rejects_unknown_task_name(pg_db: Session) -> None:
    """The contract layer rejects unknown ``task_name``; the row is not
    INSERTed and the caller receives :class:`OutboxRegistryError`."""
    from src.application.outbox.errors import OutboxRegistryError

    task_log = _make_task_log(pg_db)
    repo = OutboxRepository(pg_db)

    with pytest.raises(OutboxRegistryError):
        repo.enroll_with_task_log(task_log, _make_command("src.tasks.unknown.bad_task"))

    assert pg_db.query(OutboxEventRow).count() == 0


def test_enroll_rejects_empty_task_log_id(pg_db: Session) -> None:
    """The repository raises ``ValueError`` when ``task_log.id`` is
    ``None`` so the caller knows to flush first."""
    task_log = TaskLog(task_type="outbox.unit", status="pending")
    # Intentionally NOT flushed / committed; ``id`` is None.
    pg_db.add(task_log)
    repo = OutboxRepository(pg_db)

    with pytest.raises(ValueError, match="task_log.id"):
        repo.enroll_with_task_log(task_log, _make_command())


def test_enqueue_command_is_a_thin_wrapper(pg_db: Session) -> None:
    """``enqueue_command`` delegates to the repository and produces
    the same row shape."""
    task_log = _make_task_log(pg_db)

    outcome = enqueue_command(pg_db, _make_command(), task_log)

    assert outcome.created is True
    assert outcome.event.task_log_id == task_log.id
    assert pg_db.query(OutboxEventRow).count() == 1


# ---------------------------------------------------------------------------
# try_claim_one
# ---------------------------------------------------------------------------


def test_try_claim_one_flips_pending_to_publishing(pg_db: Session) -> None:
    """A claimable row is moved to ``publishing`` with the supplied
    ``claimed_by`` and ``lease_expires_at``."""
    task_log = _make_task_log(pg_db)
    repo = OutboxRepository(pg_db)
    repo.enroll_with_task_log(task_log, _make_command())
    _pin_next_attempt_to_past(pg_db, task_log.id)

    pinned_past = _past(datetime.now(tz=timezone.utc))
    claimed = repo.try_claim_one(claimed_by="worker-A", now=pinned_past)

    assert claimed is not None
    assert claimed.status is OutboxStatus.PUBLISHING
    assert claimed.claimed_by == "worker-A"
    assert claimed.lease_expires_at is not None
    # PG returns aware datetimes; both are tz-aware UTC.
    assert claimed.lease_expires_at >= pinned_past


def test_second_claim_one_sees_publishing_returns_none(pg_db: Session) -> None:
    """Two consecutive claims against a one-row pool — the second
    must observe the row already in ``publishing`` and return
    ``None``."""
    task_log = _make_task_log(pg_db)
    repo = OutboxRepository(pg_db)
    repo.enroll_with_task_log(task_log, _make_command())
    _pin_next_attempt_to_past(pg_db, task_log.id)

    pinned_past = _past(datetime.now(tz=timezone.utc))
    first = repo.try_claim_one(claimed_by="worker-A", now=pinned_past)
    second = repo.try_claim_one(claimed_by="worker-B", now=pinned_past)

    assert first is not None
    assert first.status is OutboxStatus.PUBLISHING
    assert second is None


def test_try_claim_one_returns_none_for_no_rows(pg_db: Session) -> None:
    """An empty pool returns ``None``."""
    repo = OutboxRepository(pg_db)
    assert repo.try_claim_one(claimed_by="worker-A") is None


# ---------------------------------------------------------------------------
# mark_published / mark_failed_to_retry / mark_failed_terminal
# ---------------------------------------------------------------------------


def _claim_pending_row(pg_db: Session, repo: OutboxRepository) -> OutboxEvent:
    """Helper: enroll + claim one row so the row is in ``publishing``."""
    task_log = _make_task_log(pg_db)
    repo.enroll_with_task_log(task_log, _make_command())
    _pin_next_attempt_to_past(pg_db, task_log.id)
    pinned_past = _past(datetime.now(tz=timezone.utc))
    claimed = repo.try_claim_one(claimed_by="worker-A", now=pinned_past)
    assert claimed is not None
    return claimed


def test_mark_published_clears_lease_and_sets_published_at(
    pg_db: Session,
) -> None:
    claimed = _claim_pending_row(pg_db, OutboxRepository(pg_db))
    repo = OutboxRepository(pg_db)

    fixed_now = datetime.now(tz=timezone.utc)
    published = repo.mark_published(claimed.event_id, now=fixed_now)

    assert published is not None
    assert published.status is OutboxStatus.PUBLISHED
    # PG returns aware datetimes; equality holds without re-attaching tzinfo.
    assert published.published_at is not None
    assert published.published_at == fixed_now
    assert published.claimed_by is None
    assert published.lease_expires_at is None


def test_mark_published_is_conditional_on_publishing(pg_db: Session) -> None:
    """Calling ``mark_published`` on a row that is still in
    ``pending`` returns ``None`` — the conditional ``WHERE`` clause
    matches nothing."""
    task_log = _make_task_log(pg_db)
    repo = OutboxRepository(pg_db)
    outcome = repo.enroll_with_task_log(task_log, _make_command())

    result = repo.mark_published(outcome.event.event_id)
    assert result is None


def test_mark_failed_to_retry_flips_publishing_to_pending(
    pg_db: Session,
) -> None:
    """A retryable failure leaves the row claimable: ``status``
    flips back to ``pending``, ``attempt_count`` increments,
    ``last_error`` and ``next_attempt_at`` are populated."""
    claimed = _claim_pending_row(pg_db, OutboxRepository(pg_db))
    repo = OutboxRepository(pg_db)

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
    # PG returns aware datetimes; equality holds directly.
    assert result.next_attempt_at == next_attempt
    assert result.claimed_by is None
    assert result.lease_expires_at is None

    # Re-claim succeeds — the row is back in pending.
    claim_now = datetime.now(tz=timezone.utc) + timedelta(seconds=20)
    reclaimed = repo.try_claim_one(claimed_by="worker-B", now=claim_now)
    assert reclaimed is not None


def test_mark_failed_terminal_flips_publishing_to_failed(pg_db: Session) -> None:
    """``mark_failed_terminal`` is the terminal path; the row is
    operationally terminal — no publisher claim can pick it up."""
    claimed = _claim_pending_row(pg_db, OutboxRepository(pg_db))
    repo = OutboxRepository(pg_db)

    result = repo.mark_failed_terminal(claimed.event_id, last_error="max attempts exceeded")

    assert result is not None
    assert result.status is OutboxStatus.FAILED
    assert result.last_error == "max attempts exceeded"
    assert result.claimed_by is None
    assert result.lease_expires_at is None

    pinned_past = _past(datetime.now(tz=timezone.utc))
    assert repo.try_claim_one(claimed_by="worker-B", now=pinned_past) is None


def test_mark_failed_to_retry_returns_none_on_pending_row(
    pg_db: Session,
) -> None:
    """``mark_failed_to_retry`` is conditional on the row being in
    ``publishing``; calling it on a ``pending`` row returns ``None``."""
    task_log = _make_task_log(pg_db)
    repo = OutboxRepository(pg_db)
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
    pg_db: Session,
    *,
    claimer: str = "worker-A",
    published_at: Optional[datetime] = None,
) -> OutboxEvent:
    """Helper: enroll → claim → publish a row in a single session."""
    task_log = _make_task_log(pg_db)
    repo = OutboxRepository(pg_db)
    repo.enroll_with_task_log(task_log, _make_command())
    _pin_next_attempt_to_past(pg_db, task_log.id)
    pinned_past = _past(datetime.now(tz=timezone.utc))
    claimed = repo.try_claim_one(claimed_by=claimer, now=pinned_past)
    assert claimed is not None
    fixed = published_at or datetime.now(tz=timezone.utc)
    published = repo.mark_published(claimed.event_id, now=fixed)
    assert published is not None
    return published


def test_count_pending_older_than_ignores_fresh_pending_rows(
    pg_db: Session,
) -> None:
    """A pending row whose ``next_attempt_at`` is in the future does
    NOT count against the backlog (the publisher has not retried it
    yet)."""
    repo = OutboxRepository(pg_db)

    task_log = _make_task_log(pg_db)
    repo.enroll_with_task_log(task_log, _make_command())

    count = repo.count_pending_older_than(seconds=300)
    assert count == 0
