"""PostgreSQL unit tests for :class:`DailySummaryAttemptRepository`.

These tests run against the project-wide ``pg_db`` fixture (a
function-scoped session on a one-shot ``alembic upgrade head`` schema
rolled back after every test). They exercise the
:class:`DailySummaryAttemptRepository` API surface against the contract
from Todo 15. They cover:

- ``claim`` — the per-date active-slot insert + lookup pair, including
  the ``created`` flag for the "someone else got there first" case.
- ``mark_*`` — the state-machine transitions enforced via conditional
  ``UPDATE`` plus the operator-facing diagnostics on the terminal paths.
- ``find_active_for_date`` / ``find_terminal_for_date`` — the read paths
  the scheduler and audit UI rely on.
- ``next_attempt_no`` — the per-date monotonic counter.
- The :data:`ALLOWED_TRANSITIONS` / :data:`TERMINAL_STATUSES` /
  :func:`is_terminal` / :func:`transition` contracts in
  :mod:`src.application.summary_attempt.state_machine`.

PostgreSQL-specific concurrency tests (concurrent ``INSERT … ON
CONFLICT`` races, partial unique index, FK ``SET NULL`` behaviour,
CHECK constraint rejection, audit ordering) live in
``tests/integration/test_daily_summary_attempt_postgres.py``.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from src.application.summary_attempt import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    DailySummaryAttemptStateError,
    DailySummaryAttemptStatus,
    can_transition,
    is_terminal,
    transition,
)
from src.application.summary_attempt.repository import (
    FAILURE_REASON_CANCELLED,
    FAILURE_REASON_TIMEOUT,
    DailySummaryAttemptRepository,
)
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.models.task_log import TaskLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_log(db: Session) -> TaskLog:
    """Insert a minimal ``TaskLog`` and flush so it has a server-assigned ``id``."""
    task_log = TaskLog(task_type="summary_attempt.unit", status="pending")
    db.add(task_log)
    db.flush()
    return task_log


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_creates_first_attempt_no_one(pg_db: Session) -> None:
    """A first claim for a date writes a row with ``attempt_no=1``,
    ``status='claimed'``, and returns ``created=True``."""
    repo = DailySummaryAttemptRepository(pg_db)
    target_date = date(2026, 9, 1)

    outcome = repo.claim(
        summary_date=target_date,
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    assert outcome.created is True
    assert outcome.attempt.summary_date == target_date
    assert outcome.attempt.attempt_no == 1
    assert outcome.attempt.status == DailySummaryAttemptStatus.CLAIMED.value
    assert outcome.attempt.triggered_by == "schedule"
    assert outcome.attempt.task_log_id is None
    assert outcome.attempt.claimed_at is not None
    assert outcome.attempt.finished_at is None

    rows = pg_db.query(DailySummaryGenerationAttempt).all()
    assert len(rows) == 1
    assert rows[0].id == outcome.attempt.id


def test_claim_with_existing_active_returns_created_false(pg_db: Session) -> None:
    """A second claim for the same date with an active row in place
    returns ``created=False`` pointing at the existing row — the
    caller is told "back off, someone else owns this date"."""
    repo = DailySummaryAttemptRepository(pg_db)
    target_date = date(2026, 9, 1)

    first = repo.claim(
        summary_date=target_date,
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    second = repo.claim(
        summary_date=target_date,
        attempt_no=2,
        triggered_by="manual",
        task_log_id=None,
    )
    pg_db.commit()

    assert first.created is True
    assert second.created is False
    assert second.attempt.id == first.attempt.id
    assert second.attempt.attempt_no == 1

    rows = pg_db.query(DailySummaryGenerationAttempt).all()
    assert len(rows) == 1


def test_claim_rejects_non_positive_attempt_no(pg_db: Session) -> None:
    """``attempt_no`` must be positive; ``0`` and negatives raise
    :class:`ValueError` before any INSERT is issued."""
    repo = DailySummaryAttemptRepository(pg_db)
    with pytest.raises(ValueError, match="attempt_no"):
        repo.claim(
            summary_date=date(2026, 9, 1),
            attempt_no=0,
            triggered_by=None,
            task_log_id=None,
        )
    assert pg_db.query(DailySummaryGenerationAttempt).count() == 0


# ---------------------------------------------------------------------------
# Retry after a terminal status
# ---------------------------------------------------------------------------


def test_claim_after_supersede_allows_new_attempt(pg_db: Session) -> None:
    """Once an active attempt is superseded the per-date active slot is
    free again, so the next call inserts a fresh row with the next
    ``attempt_no``."""
    repo = DailySummaryAttemptRepository(pg_db)
    target_date = date(2026, 9, 1)

    first = repo.claim(
        summary_date=target_date,
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    superseded = repo.mark_superseded(first.attempt.id)
    assert superseded is not None
    assert superseded.status == DailySummaryAttemptStatus.SUPERSEDED.value
    pg_db.commit()

    second = repo.claim(
        summary_date=target_date,
        attempt_no=2,
        triggered_by="manual",
        task_log_id=None,
    )
    pg_db.commit()

    assert second.created is True
    assert second.attempt.attempt_no == 2
    assert second.attempt.id != first.attempt.id

    rows = pg_db.query(DailySummaryGenerationAttempt).all()
    assert sorted(row.attempt_no for row in rows) == [1, 2]


def test_claim_after_failed_allows_new_attempt(pg_db: Session) -> None:
    """A ``failed`` row also leaves the active slot, so the next claim
    inserts a fresh row whose diagnostics for the failed run are
    preserved."""
    repo = DailySummaryAttemptRepository(pg_db)
    target_date = date(2026, 9, 1)

    first = repo.claim(
        summary_date=target_date,
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    repo.mark_running(first.attempt.id)
    failed = repo.mark_failed(
        first.attempt.id,
        error_type="LLMTimeoutError",
        last_error="upstream timeout after 30s",
        failure_reason="llm_error",
    )
    assert failed is not None
    pg_db.commit()

    second = repo.claim(
        summary_date=target_date,
        attempt_no=2,
        triggered_by="retry",
        task_log_id=None,
    )
    pg_db.commit()

    assert second.created is True
    assert second.attempt.attempt_no == 2

    history = repo.find_terminal_for_date(target_date)
    assert len(history) == 1
    assert history[0].status == DailySummaryAttemptStatus.FAILED.value
    assert history[0].failure_reason == "llm_error"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_state_machine_terminal_statuses_classification() -> None:
    """``is_terminal`` returns ``True`` for the five terminal statuses
    and ``False`` for the three active ones."""
    expected_terminal = {
        DailySummaryAttemptStatus.SUCCEEDED,
        DailySummaryAttemptStatus.FAILED,
        DailySummaryAttemptStatus.CANCELLED,
        DailySummaryAttemptStatus.TIMED_OUT,
        DailySummaryAttemptStatus.SUPERSEDED,
    }
    expected_active = {
        DailySummaryAttemptStatus.CLAIMED,
        DailySummaryAttemptStatus.QUEUED,
        DailySummaryAttemptStatus.RUNNING,
    }

    assert TERMINAL_STATUSES == expected_terminal
    assert ACTIVE_STATUSES == expected_active

    for state in expected_terminal:
        assert is_terminal(state) is True, state
    for state in expected_active:
        assert is_terminal(state) is False, state


def test_state_machine_allowed_transitions_table_matches_spec() -> None:
    """The transition table is the canonical contract and must match
    the state-machine diagram in the module docstring."""
    expected: frozenset[tuple[DailySummaryAttemptStatus, DailySummaryAttemptStatus]] = frozenset(
        {
            (DailySummaryAttemptStatus.CLAIMED, DailySummaryAttemptStatus.QUEUED),
            (DailySummaryAttemptStatus.CLAIMED, DailySummaryAttemptStatus.RUNNING),
            (DailySummaryAttemptStatus.CLAIMED, DailySummaryAttemptStatus.CANCELLED),
            (DailySummaryAttemptStatus.CLAIMED, DailySummaryAttemptStatus.SUPERSEDED),
            (DailySummaryAttemptStatus.QUEUED, DailySummaryAttemptStatus.RUNNING),
            (DailySummaryAttemptStatus.QUEUED, DailySummaryAttemptStatus.CANCELLED),
            (DailySummaryAttemptStatus.QUEUED, DailySummaryAttemptStatus.SUPERSEDED),
            (DailySummaryAttemptStatus.QUEUED, DailySummaryAttemptStatus.TIMED_OUT),
            (DailySummaryAttemptStatus.RUNNING, DailySummaryAttemptStatus.SUCCEEDED),
            (DailySummaryAttemptStatus.RUNNING, DailySummaryAttemptStatus.FAILED),
            (DailySummaryAttemptStatus.RUNNING, DailySummaryAttemptStatus.CANCELLED),
            (DailySummaryAttemptStatus.RUNNING, DailySummaryAttemptStatus.TIMED_OUT),
            (DailySummaryAttemptStatus.RUNNING, DailySummaryAttemptStatus.SUPERSEDED),
        }
    )
    assert ALLOWED_TRANSITIONS == expected


def test_state_machine_illegal_transitions_raise() -> None:
    """A transition not in :data:`ALLOWED_TRANSITIONS` raises
    :class:`DailySummaryAttemptStateError`; ``can_transition`` returns
    ``False`` without raising."""
    with pytest.raises(DailySummaryAttemptStateError):
        transition(DailySummaryAttemptStatus.CLAIMED, DailySummaryAttemptStatus.SUCCEEDED)
    claimed_to_succeeded = (
        DailySummaryAttemptStatus.CLAIMED,
        DailySummaryAttemptStatus.SUCCEEDED,
    )
    assert can_transition(*claimed_to_succeeded) is False

    with pytest.raises(DailySummaryAttemptStateError):
        transition(
            DailySummaryAttemptStatus.CLAIMED,
            DailySummaryAttemptStatus.TIMED_OUT,
        )

    # Terminal → anything is illegal.
    with pytest.raises(DailySummaryAttemptStateError):
        transition(DailySummaryAttemptStatus.SUCCEEDED, DailySummaryAttemptStatus.RUNNING)
    with pytest.raises(DailySummaryAttemptStateError):
        transition(DailySummaryAttemptStatus.FAILED, DailySummaryAttemptStatus.CLAIMED)


def test_state_machine_expected_from_guard_rejects_mismatch() -> None:
    """``expected_from`` guards against the caller passing an
    internally inconsistent pair; a mismatch raises
    :class:`DailySummaryAttemptStateError`."""
    with pytest.raises(DailySummaryAttemptStateError, match="expected_from"):
        transition(
            DailySummaryAttemptStatus.CLAIMED,
            DailySummaryAttemptStatus.SUCCEEDED,
            expected_from=(DailySummaryAttemptStatus.RUNNING,),
        )


# ---------------------------------------------------------------------------
# mark_* terminal paths
# ---------------------------------------------------------------------------


def test_mark_succeeded_sets_finished_at(pg_db: Session) -> None:
    """``mark_succeeded`` flips ``running`` → ``succeeded`` and stamps
    ``finished_at``."""
    repo = DailySummaryAttemptRepository(pg_db)
    first = repo.claim(
        summary_date=date(2026, 9, 1),
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    repo.mark_running(first.attempt.id)
    succeeded = repo.mark_succeeded(first.attempt.id)
    pg_db.commit()

    assert succeeded is not None
    assert succeeded.status == DailySummaryAttemptStatus.SUCCEEDED.value
    assert succeeded.finished_at is not None
    assert succeeded.error_type is None
    assert succeeded.last_error is None
    assert succeeded.failure_reason is None


def test_mark_succeeded_illegal_source_returns_none(pg_db: Session) -> None:
    """``mark_succeeded`` on a ``claimed`` row returns ``None``: the
    conditional ``UPDATE`` matches no rows because
    ``claimed → succeeded`` is not in the transition table."""
    repo = DailySummaryAttemptRepository(pg_db)
    first = repo.claim(
        summary_date=date(2026, 9, 1),
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    result = repo.mark_succeeded(first.attempt.id)
    pg_db.commit()

    assert result is None

    refreshed = repo.find_active_for_date(date(2026, 9, 1))
    assert refreshed is not None
    assert refreshed.status == DailySummaryAttemptStatus.CLAIMED.value


def test_mark_failed_records_error_type_and_reason(pg_db: Session) -> None:
    """``mark_failed`` writes ``error_type``, ``last_error``,
    ``failure_reason`` and stamps ``finished_at``."""
    repo = DailySummaryAttemptRepository(pg_db)
    first = repo.claim(
        summary_date=date(2026, 9, 1),
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    repo.mark_running(first.attempt.id)
    failed = repo.mark_failed(
        first.attempt.id,
        error_type="JSONDecodeError",
        last_error="unexpected token at line 3",
        failure_reason="llm_error",
    )
    pg_db.commit()

    assert failed is not None
    assert failed.status == DailySummaryAttemptStatus.FAILED.value
    assert failed.error_type == "JSONDecodeError"
    assert failed.last_error == "unexpected token at line 3"
    assert failed.failure_reason == "llm_error"
    assert failed.finished_at is not None


def test_mark_cancelled_records_cancellation(pg_db: Session) -> None:
    """``mark_cancelled`` writes ``status='cancelled'``, the supplied
    ``last_error`` and stamps ``finished_at``. ``error_type`` stays
    ``None`` (operator action is not a defect) and ``failure_reason``
    is fixed to ``cancelled``."""
    repo = DailySummaryAttemptRepository(pg_db)
    first = repo.claim(
        summary_date=date(2026, 9, 1),
        attempt_no=1,
        triggered_by="manual",
        task_log_id=None,
    )
    pg_db.commit()

    cancelled = repo.mark_cancelled(
        first.attempt.id,
        last_error="operator pressed cancel",
    )
    pg_db.commit()

    assert cancelled is not None
    assert cancelled.status == DailySummaryAttemptStatus.CANCELLED.value
    assert cancelled.last_error == "operator pressed cancel"
    assert cancelled.error_type is None
    assert cancelled.failure_reason == FAILURE_REASON_CANCELLED
    assert cancelled.finished_at is not None


def test_mark_timed_out_distinct_from_failed(pg_db: Session) -> None:
    """``mark_timed_out`` writes ``status='timed_out'`` (different
    from ``failed``) with ``failure_reason='timeout'``. ``claimed``
    has no ``timed_out`` edge — a row that never reached a worker is
    superseded, not timed out."""
    repo = DailySummaryAttemptRepository(pg_db)
    first = repo.claim(
        summary_date=date(2026, 9, 1),
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    # ``claimed`` → ``timed_out`` is not allowed.
    assert repo.mark_timed_out(first.attempt.id, last_error="watchdog") is None
    pg_db.commit()

    # Move into ``queued`` and the timed-out edge opens.
    queued = repo.mark_queued(first.attempt.id)
    assert queued is not None
    pg_db.commit()

    timed_out = repo.mark_timed_out(first.attempt.id, last_error="worker killed after 5m")
    pg_db.commit()

    assert timed_out is not None
    assert timed_out.status == DailySummaryAttemptStatus.TIMED_OUT.value
    assert timed_out.last_error == "worker killed after 5m"
    assert timed_out.failure_reason == FAILURE_REASON_TIMEOUT
    assert timed_out.error_type is None
    assert timed_out.finished_at is not None
    assert timed_out.status != DailySummaryAttemptStatus.FAILED.value


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_next_attempt_no_returns_max_plus_one_or_one(pg_db: Session) -> None:
    """``next_attempt_no`` returns 1 when no rows exist for the date
    and ``max(attempt_no) + 1`` otherwise."""
    repo = DailySummaryAttemptRepository(pg_db)
    target_date = date(2026, 9, 1)

    assert repo.next_attempt_no(target_date) == 1

    for _ in (1, 2, 3):
        repo.claim(
            summary_date=target_date,
            attempt_no=repo.next_attempt_no(target_date),
            triggered_by="manual",
            task_log_id=None,
        )
        repo.mark_superseded(_last_attempt_id(pg_db))
        pg_db.commit()

    assert repo.next_attempt_no(target_date) == 4

    # Other dates have their own counter.
    other_date = date(2026, 9, 2)
    assert repo.next_attempt_no(other_date) == 1


def test_find_active_returns_only_active_statuses(pg_db: Session) -> None:
    """``find_active_for_date`` returns exactly the row whose status is
    in :data:`ACTIVE_STATUSES`; every terminal row is excluded."""
    repo = DailySummaryAttemptRepository(pg_db)

    # Use distinct summary_dates so the partial unique index does not
    # block attempts that want to remain active in parallel. The query
    # path under test does not care which dates participate; it only
    # cares about the status filter.
    active_dates = {
        DailySummaryAttemptStatus.CLAIMED: date(2026, 9, 1),
        DailySummaryAttemptStatus.QUEUED: date(2026, 9, 2),
        DailySummaryAttemptStatus.RUNNING: date(2026, 9, 3),
    }
    terminal_dates = {
        DailySummaryAttemptStatus.SUCCEEDED: date(2026, 9, 4),
        DailySummaryAttemptStatus.FAILED: date(2026, 9, 5),
        DailySummaryAttemptStatus.CANCELLED: date(2026, 9, 6),
        DailySummaryAttemptStatus.TIMED_OUT: date(2026, 9, 7),
        DailySummaryAttemptStatus.SUPERSEDED: date(2026, 9, 8),
    }

    for status, day in active_dates.items():
        repo.claim(
            summary_date=day,
            attempt_no=1,
            triggered_by="manual",
            task_log_id=None,
        )
        attempt_id = _last_attempt_id(pg_db)
        _drive_to(pg_db, attempt_id, status)
        pg_db.commit()

    for status, day in terminal_dates.items():
        repo.claim(
            summary_date=day,
            attempt_no=1,
            triggered_by="manual",
            task_log_id=None,
        )
        attempt_id = _last_attempt_id(pg_db)
        _drive_to(pg_db, attempt_id, status)
        pg_db.commit()

    # ``find_active_for_date`` for each active-status date returns the
    # row in the active set and excludes terminal rows entirely.
    for status, day in active_dates.items():
        active = repo.find_active_for_date(day)
        assert active is not None, day
        assert active.status == status.value
        assert active.status in {s.value for s in ACTIVE_STATUSES}

    # ``find_terminal_for_date`` for each terminal-status date returns
    # the single terminal row, ordered ascending.
    for status, day in terminal_dates.items():
        terminal = repo.find_terminal_for_date(day)
        assert len(terminal) == 1
        assert terminal[0].status == status.value


def test_find_terminal_returns_only_terminal_statuses(pg_db: Session) -> None:
    """``find_terminal_for_date`` returns every terminal row, oldest
    first; active rows are excluded."""
    repo = DailySummaryAttemptRepository(pg_db)
    target_date = date(2026, 9, 1)

    statuses_by_no = [
        DailySummaryAttemptStatus.SUCCEEDED,
        DailySummaryAttemptStatus.FAILED,
        DailySummaryAttemptStatus.CANCELLED,
        DailySummaryAttemptStatus.TIMED_OUT,
        DailySummaryAttemptStatus.SUPERSEDED,
    ]
    for index, status in enumerate(statuses_by_no, start=1):
        repo.claim(
            summary_date=target_date,
            attempt_no=index,
            triggered_by="manual",
            task_log_id=None,
        )
        attempt_id = _last_attempt_id(pg_db)
        _drive_to(pg_db, attempt_id, status)
        pg_db.commit()

    terminal = repo.find_terminal_for_date(target_date)
    assert {row.status for row in terminal} == {
        DailySummaryAttemptStatus.SUCCEEDED.value,
        DailySummaryAttemptStatus.FAILED.value,
        DailySummaryAttemptStatus.CANCELLED.value,
        DailySummaryAttemptStatus.TIMED_OUT.value,
        DailySummaryAttemptStatus.SUPERSEDED.value,
    }
    assert [row.attempt_no for row in terminal] == [1, 2, 3, 4, 5]


def test_running_records_events_count_and_input_token_estimate(pg_db: Session) -> None:
    """``mark_running`` snapshots ``events_count`` /
    ``input_token_estimate`` so a post-mortem can tell what the LLM
    was called with."""
    repo = DailySummaryAttemptRepository(pg_db)
    first = repo.claim(
        summary_date=date(2026, 9, 1),
        attempt_no=1,
        triggered_by="schedule",
        task_log_id=None,
    )
    pg_db.commit()

    running = repo.mark_running(
        first.attempt.id,
        events_count=37,
        input_token_estimate=4096,
    )
    pg_db.commit()

    assert running is not None
    assert running.status == DailySummaryAttemptStatus.RUNNING.value
    assert running.events_count == 37
    assert running.input_token_estimate == 4096


# ---------------------------------------------------------------------------
# Internal helpers (test-only)
# ---------------------------------------------------------------------------


def _last_attempt_id(pg_db: Session) -> int:
    """Return the highest ``id`` in the attempt table for the active test."""
    row = (
        pg_db.query(DailySummaryGenerationAttempt)
        .order_by(DailySummaryGenerationAttempt.id.desc())
        .first()
    )
    assert row is not None, "no attempt rows in the test session"
    return row.id


def _drive_to(
    pg_db: Session,
    attempt_id: int,
    target: DailySummaryAttemptStatus,
) -> None:
    """Drive an attempt through the state machine to ``target``.

    The state-machine edges are walked greedily (claimed → queued →
    running → target); the test only invokes this for targets that
    are reachable from ``claimed`` via the spec's transition table.
    """
    repo = DailySummaryAttemptRepository(pg_db)

    if target == DailySummaryAttemptStatus.CLAIMED:
        return

    if target == DailySummaryAttemptStatus.QUEUED:
        assert repo.mark_queued(attempt_id) is not None
        return

    assert repo.mark_queued(attempt_id) is not None
    if target == DailySummaryAttemptStatus.RUNNING:
        assert repo.mark_running(attempt_id) is not None
        return

    assert repo.mark_running(attempt_id) is not None

    if target == DailySummaryAttemptStatus.SUCCEEDED:
        assert repo.mark_succeeded(attempt_id) is not None
        return
    if target == DailySummaryAttemptStatus.FAILED:
        assert (
            repo.mark_failed(
                attempt_id,
                error_type="RuntimeError",
                last_error="forced",
                failure_reason="llm_error",
            )
            is not None
        )
        return
    if target == DailySummaryAttemptStatus.CANCELLED:
        assert repo.mark_cancelled(attempt_id, last_error="forced") is not None
        return
    if target == DailySummaryAttemptStatus.TIMED_OUT:
        assert repo.mark_timed_out(attempt_id, last_error="forced") is not None
        return
    if target == DailySummaryAttemptStatus.SUPERSEDED:
        assert repo.mark_superseded(attempt_id) is not None
        return

    raise AssertionError(f"unhandled target {target!r}")
