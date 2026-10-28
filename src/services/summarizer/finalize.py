"""Attempt lifecycle and the terminal publication step."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from src.application.ports.attempt_repository import AttemptRepositoryPort, ClaimedAttempt
from src.core.contracts import WEBHOOK_SIGNATURE_VERSION
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.models.webhook_config import WebhookConfig
from src.services.summarizer.contracts import WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED
from src.services.webhook_subscription import webhook_subscribes


# --- finalize.py ---
def find_subscribed_webhooks(db: Session) -> list[int]:
    """Return the ids of enabled ``WebhookConfig`` rows that subscribe to the event."""
    hooks = db.query(WebhookConfig).filter(WebhookConfig.enabled.is_(True)).all()
    return [
        int(hook.id)
        for hook in hooks
        if webhook_subscribes(
            hook,
            event_type=WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
            version=WEBHOOK_SIGNATURE_VERSION,
        )
    ]


# --- lifecycle.py ---
def claim_attempt(
    repo: AttemptRepositoryPort,
    *,
    summary_date: date,
    triggered_by: str | None,
    task_log_id: int | None,
) -> ClaimedAttempt:
    """Claim the per-date active slot via the supplied repository."""
    attempt_no = repo.next_attempt_no(summary_date)
    return repo.claim(
        summary_date=summary_date,
        attempt_no=attempt_no,
        triggered_by=triggered_by,
        task_log_id=task_log_id,
    )


# --- lifecycle.py ---
def mark_running(
    repo: AttemptRepositoryPort,
    *,
    attempt_id: int,
    events_count: int | None = None,
    input_token_estimate: int | None = None,
) -> DailySummaryGenerationAttempt | None:
    """Flip ``claimed`` / ``queued`` → ``running`` on the attempt row."""
    return repo.mark_running(
        attempt_id,
        events_count=events_count,
        input_token_estimate=input_token_estimate,
    )


# --- lifecycle.py ---
def attempt_already_running(
    repo: AttemptRepositoryPort, summary_date: date
) -> DailySummaryGenerationAttempt | None:
    """Return the active attempt row if one already holds the date slot."""
    return repo.find_active_for_date(summary_date)


# --- lifecycle.py ---
def mark_failed(
    repo: AttemptRepositoryPort,
    *,
    attempt_id: int,
    error_type: str,
    last_error: str,
    failure_reason: str,
) -> DailySummaryGenerationAttempt | None:
    """Move ``running`` → ``failed`` with operator-facing diagnostics."""
    return repo.mark_failed(
        attempt_id,
        error_type=error_type,
        last_error=last_error,
        failure_reason=failure_reason,
    )


# --- lifecycle.py ---
def mark_cancelled(
    repo: AttemptRepositoryPort,
    *,
    attempt_id: int,
    last_error: str,
) -> DailySummaryGenerationAttempt | None:
    """Move ``claimed`` / ``queued`` / ``running`` → ``cancelled``."""
    return repo.mark_cancelled(attempt_id, last_error=last_error)


__all__ = [
    "attempt_already_running",
    "claim_attempt",
    "find_subscribed_webhooks",
    "mark_cancelled",
    "mark_failed",
    "mark_running",
]
