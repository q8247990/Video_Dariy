"""Daily-summary generation attempt state and persistence.

This package owns the lifecycle of *one attempt* to generate the daily
summary for one date. It exists to unpick the three responsibilities
that ``daily_summary`` used to carry at once (final result + generation
lock + implicit failure state) — see
:mod:`src.models.daily_summary_attempt` for the full rationale.

Submodules
==========

``errors``
    :class:`DailySummaryAttemptError` base plus
    :class:`DailySummaryAttemptStateError` for illegal transitions.
    Deliberately separate from the outbox error hierarchy.

``state_machine``
    :class:`DailySummaryAttemptStatus` (8 values),
    :data:`ALLOWED_TRANSITIONS` (13 edges),
    :data:`TERMINAL_STATUSES` / :data:`ACTIVE_STATUSES`, and the
    :func:`can_transition` / :func:`transition` / :func:`is_terminal` /
    :func:`allowed_sources_for` helpers.

``repository``
    :class:`DailySummaryAttemptRepository` — the SQLAlchemy bridge.
    Owns the ``claim`` path (``INSERT … ON CONFLICT DO NOTHING``
    against the per-date active partial unique index), the ``mark_*``
    conditional-UPDATE transitions, and the audit read paths. Returns
    :class:`DailySummaryAttemptOutcome` from ``claim`` so the caller can
    tell "I own this date" from "someone else already does".
"""

from __future__ import annotations

from src.application.summary_attempt.errors import (
    DailySummaryAttemptError,
    DailySummaryAttemptStateError,
)
from src.application.summary_attempt.repository import (
    FAILURE_REASON_CANCELLED,
    FAILURE_REASON_TIMEOUT,
    DailySummaryAttemptOutcome,
    DailySummaryAttemptRepository,
)
from src.application.summary_attempt.state_machine import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    DailySummaryAttemptStatus,
    allowed_sources_for,
    can_transition,
    is_terminal,
    transition,
)

__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "FAILURE_REASON_CANCELLED",
    "FAILURE_REASON_TIMEOUT",
    "TERMINAL_STATUSES",
    "DailySummaryAttemptError",
    "DailySummaryAttemptOutcome",
    "DailySummaryAttemptRepository",
    "DailySummaryAttemptStateError",
    "DailySummaryAttemptStatus",
    "allowed_sources_for",
    "can_transition",
    "is_terminal",
    "transition",
]
