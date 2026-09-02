"""Daily-summary attempt lifecycle helpers — pure DB operations.

The state machine and the attempt repository live in
:mod:`src.application.summary_attempt`; this module is a thin
adapter that takes the *repository instance* (constructed by the
caller in :mod:`src.tasks.summarizer`) and exposes the four
transitions the summarizer actually drives.

The :class:`src.application.summary_attempt.repository.DailySummaryAttemptRepository`
and its outcome / row types are intentionally **not** imported
here — the architecture boundary rule forbids
``src.services.*`` from importing use-case modules. The functions
below take the repository as their first argument and return
``Any`` so this layer stays free of
:mod:`src.application.*` imports.

* :func:`claim_attempt` — claim the per-date active slot before any
  expensive LLM work starts.
* :func:`mark_running` — flip the row to ``running`` so the audit
  log can show the attempt started.
* :func:`mark_failed` — record a failure (LLM error / parse error /
  unexpected exception) and preserve any previously-published
  ``daily_summary``.
* :func:`mark_cancelled` — record an operator-initiated cancel.

The **success** transition is owned by the publish use case (see
:mod:`src.application.summary_publication`); the summarizer never
calls :meth:`mark_succeeded` directly.

All helpers operate on the caller's ``Session`` and the
caller-supplied repository instance; the caller owns the commit.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional


def claim_attempt(
    repo: Any,
    *,
    summary_date: date,
    triggered_by: Optional[str],
    task_log_id: Optional[int],
) -> Any:
    """Claim the per-date active slot via the supplied repository."""
    attempt_no = repo.next_attempt_no(summary_date)
    return repo.claim(
        summary_date=summary_date,
        attempt_no=attempt_no,
        triggered_by=triggered_by,
        task_log_id=task_log_id,
    )


def mark_running(
    repo: Any,
    *,
    attempt_id: int,
    events_count: Optional[int] = None,
    input_token_estimate: Optional[int] = None,
) -> Any:
    """Flip ``claimed`` / ``queued`` → ``running`` on the attempt row."""
    return repo.mark_running(
        attempt_id,
        events_count=events_count,
        input_token_estimate=input_token_estimate,
    )


def attempt_already_running(repo: Any, summary_date: date) -> Any:
    """Return the active attempt row if one already holds the date slot."""
    return repo.find_active_for_date(summary_date)


def mark_failed(
    repo: Any,
    *,
    attempt_id: int,
    error_type: str,
    last_error: str,
    failure_reason: str,
) -> Any:
    """Move ``running`` → ``failed`` with operator-facing diagnostics."""
    return repo.mark_failed(
        attempt_id,
        error_type=error_type,
        last_error=last_error,
        failure_reason=failure_reason,
    )


def mark_cancelled(
    repo: Any,
    *,
    attempt_id: int,
    last_error: str,
) -> Any:
    """Move ``claimed`` / ``queued`` / ``running`` → ``cancelled``."""
    return repo.mark_cancelled(attempt_id, last_error=last_error)


__all__ = [
    "attempt_already_running",
    "claim_attempt",
    "mark_cancelled",
    "mark_failed",
    "mark_running",
]
