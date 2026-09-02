"""Daily-summary attempt error hierarchy.

Mirrors the shape of :mod:`src.application.outbox.errors` — a base
class so callers can catch every attempt-contract violation uniformly,
plus a concrete subclass for illegal state transitions. Each error
carries a ``payload`` keyword so the offending values can be attached
without interpolating caller data into the message.

The hierarchy is deliberately **separate** from the outbox errors:
the outbox is about broker publication and this package is about
business generation state. Sharing an exception base would force
``except OutboxError`` handlers in the publisher to also swallow daily
summary attempt failures, which is exactly the coupling the two-table
split (ADR §1) is meant to avoid.
"""

from __future__ import annotations

from typing import Any, Optional


class DailySummaryAttemptError(Exception):
    """Base class for every daily-summary attempt contract violation.

    Catch this in a task / use case when you want a uniform "this
    attempt transition was refused" path. Never ``str()`` the
    ``payload`` into an operator-visible message: it may carry
    caller-supplied data.
    """

    def __init__(self, message: str, *, payload: Optional[Any] = None) -> None:
        super().__init__(message)
        self.payload = payload


class DailySummaryAttemptStateError(DailySummaryAttemptError):
    """Raised when an attempt state transition is not in the allowed set.

    The transition table lives in
    :mod:`src.application.summary_attempt.state_machine`. The task
    layer translates this exception into "the attempt is already
    terminal; do not touch it" rather than "the caller was buggy":
    a late-arriving cancel for an already-succeeded attempt is a
    normal race, not an error to surface to the operator.
    """


__all__ = [
    "DailySummaryAttemptError",
    "DailySummaryAttemptStateError",
]
