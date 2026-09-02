"""Daily-summary attempt state machine.

The states and transitions mirror the *shape* of the outbox state
machine (ADR ``docs/adr/0011-transactional-outbox-and-task-lifecycle.md``
§4) — a small frozen transition table plus ``can_transition`` /
``transition`` / ``is_terminal`` helpers — but the **values are a
separate enum**. An outbox row tracks broker publication
(``pending → publishing → published``); an attempt row tracks business
generation (``claimed → queued → running → succeeded``). They share no
state name and no transition, so merging them would produce a state
machine where half the transitions are illegal for half the rows.

Transition graph::

    claimed ──> queued ──> running ──> succeeded          (happy path)
       │           │          ├──────> failed             (LLM / parse error)
       │           │          ├──────> cancelled          (operator cancel)
       │           ├──────────┼──────> timed_out          (watchdog)
       └───────────┴──────────┴──────> superseded         (newer attempt took over)

Notes on the graph:

- ``claimed → running`` exists (skipping ``queued``) because an
  in-process generation path never round-trips through the broker;
  only the outbox-dispatched path passes through ``queued``.
- ``claimed`` has **no** ``timed_out`` edge: a row that never left
  ``claimed`` was never handed to a worker, so the watchdog treats it
  as ``superseded`` (a newer attempt takes the slot) rather than
  claiming a timeout that never had a deadline.
- The five terminal states have **no** outgoing edges at all. Unlike
  the outbox's ``failed → pending`` operator reset, a terminal attempt
  is never revived: retrying inserts a *new* row with the next
  ``attempt_no``, so the failure diagnostics of the old attempt stay
  readable forever.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from src.application.summary_attempt.errors import DailySummaryAttemptStateError


class DailySummaryAttemptStatus(str, Enum):
    """Lifecycle states for one daily-summary generation attempt.

    Stored in ``daily_summary_generation_attempt.status`` as TEXT with
    a CHECK constraint listing exactly these eight values. The string
    values are part of the persisted schema; renaming one requires a
    data migration.
    """

    CLAIMED = "claimed"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SUPERSEDED = "superseded"


#: The frozen transition table. Membership in this set is the single
#: source of truth; :func:`can_transition` and :func:`transition` are
#: thin accessors over it.
ALLOWED_TRANSITIONS: frozenset[tuple[DailySummaryAttemptStatus, DailySummaryAttemptStatus]] = (
    frozenset(
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
)

#: States with no outgoing transitions. A terminal attempt is never
#: revived; a retry creates a new row with the next ``attempt_no``.
TERMINAL_STATUSES: frozenset[DailySummaryAttemptStatus] = frozenset(
    {
        DailySummaryAttemptStatus.SUCCEEDED,
        DailySummaryAttemptStatus.FAILED,
        DailySummaryAttemptStatus.CANCELLED,
        DailySummaryAttemptStatus.TIMED_OUT,
        DailySummaryAttemptStatus.SUPERSEDED,
    }
)

#: States that hold the per-date active slot. Matches the partial
#: unique index predicate in
#: :mod:`src.models.daily_summary_attempt` verbatim.
ACTIVE_STATUSES: frozenset[DailySummaryAttemptStatus] = frozenset(
    {
        DailySummaryAttemptStatus.CLAIMED,
        DailySummaryAttemptStatus.QUEUED,
        DailySummaryAttemptStatus.RUNNING,
    }
)


def can_transition(
    current: DailySummaryAttemptStatus,
    target: DailySummaryAttemptStatus,
) -> bool:
    """Return True if ``current → target`` is in :data:`ALLOWED_TRANSITIONS`."""
    return (current, target) in ALLOWED_TRANSITIONS


def is_terminal(state: DailySummaryAttemptStatus) -> bool:
    """Return True if ``state`` has no outgoing transitions.

    Unlike the outbox helper of the same name, this one is total: all
    five terminal attempt statuses are permanently terminal, so
    ``is_terminal(s)`` is exactly ``s in TERMINAL_STATUSES``.
    """
    return state in TERMINAL_STATUSES


def allowed_sources_for(
    target: DailySummaryAttemptStatus,
) -> frozenset[DailySummaryAttemptStatus]:
    """Return every state that may legally transition into ``target``.

    The repository uses this reverse lookup to build the conditional
    ``UPDATE … WHERE status IN (:sources)`` guard for each ``mark_*``
    method, so the SQL guard and the transition table can never drift:
    a ``mark_succeeded`` on a ``claimed`` row matches zero rows because
    ``claimed → succeeded`` is not in the table.
    """
    return frozenset(source for source, candidate in ALLOWED_TRANSITIONS if candidate is target)


def transition(
    current: DailySummaryAttemptStatus,
    target: DailySummaryAttemptStatus,
    *,
    expected_from: Iterable[DailySummaryAttemptStatus] | None = None,
) -> DailySummaryAttemptStatus:
    """Validate a transition and return ``target``.

    ``expected_from`` is the caller's assertion about the state the row
    is in right now (e.g. the summarizer's "I only ever finish a row I
    put into ``running``"). The final check still consults
    :data:`ALLOWED_TRANSITIONS`; ``expected_from`` guards against the
    caller passing an internally inconsistent pair.

    Raises :class:`DailySummaryAttemptStateError` when the transition
    is not in the table, or when ``expected_from`` is supplied and
    ``current`` is not in it.
    """
    if expected_from is not None and current not in frozenset(expected_from):
        raise DailySummaryAttemptStateError(
            f"daily summary attempt transition {current.value} → {target.value} "
            f"violates expected_from={sorted(s.value for s in expected_from)}",
            payload={
                "current": current.value,
                "target": target.value,
                "expected_from": sorted(s.value for s in expected_from),
            },
        )
    if not can_transition(current, target):
        raise DailySummaryAttemptStateError(
            f"daily summary attempt transition {current.value} → {target.value} is not allowed",
            payload={"current": current.value, "target": target.value},
        )
    return target


__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATUSES",
    "DailySummaryAttemptStatus",
    "allowed_sources_for",
    "can_transition",
    "is_terminal",
    "transition",
]
