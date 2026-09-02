"""Final-stage helpers — webhook discovery + payload builders.

This module is the seam between the per-day summary generation
(``generation`` / ``normalization``) and the publication concerns
the Celery task owns (publish-daily-summary use case + webhook
fan-out). The two endpoints:

* :func:`find_subscribed_webhooks` — list of ``WebhookConfig.id``
  values matching the daily-summary event. The publish use case
  iterates over this list to enroll one ``OutboxCommand`` per
  subscriber (Todo 16's atomicity contract).
* :func:`build_webhook_payload` — legacy envelope the consumer-side
  :func:`is_webhook_event_payload` guard accepts (``event`` /
  ``version`` / ``generated_at`` / ``data`` with ``data.date``,
  ``data.summary_title``, etc.).

The actual ``publish_daily_summary`` call lives in the Celery task
wrapper (``src.tasks.summarizer``) — keeping this layer free of
:mod:`src.application.*` imports. See the task docstring for the
transaction discipline.

The :class:`WebhookConfig` model itself is just an ORM row, not a
use case, so importing it from this module is allowed by the
architecture rule.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.models.webhook_config import WebhookConfig
from src.services.summarizer.constants import WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED
from src.services.webhook_payload import build_webhook_event_payload
from src.services.webhook_subscription import webhook_subscribes


def find_subscribed_webhooks(db: Session) -> list[int]:
    """Return the ids of enabled ``WebhookConfig`` rows that subscribe to the event."""
    hooks = db.query(WebhookConfig).filter(WebhookConfig.enabled.is_(True)).all()
    return [
        int(hook.id)
        for hook in hooks
        if webhook_subscribes(hook, event_type=WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED, version="1.0")
    ]


def build_webhook_payload(
    *,
    target_date: date,
    summary_title: str,
    overall_summary: str,
    subject_sections: list[dict[str, Any]],
    attention_items: list[dict[str, Any]],
    event_count: int,
) -> dict[str, Any]:
    """Build the legacy JSON envelope the webhook subscribers expect.

    The publish use case (Todo 16) builds its own envelope via
    :func:`publish_daily_summary` (one ``OutboxEvent`` per
    subscriber); this is the legacy single-command shape the
    dispatcher fan-out uses. Both envelopes coexist today; the
    consumer-side :func:`is_webhook_event_payload` guard accepts
    this shape, so the legacy ``dispatch_webhook`` test contract
    keeps working.
    """
    return build_webhook_event_payload(
        WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
        {
            "date": str(target_date),
            "summary_title": summary_title,
            "overall_summary": overall_summary,
            "subject_sections": subject_sections,
            "attention_items": attention_items,
            "event_count": event_count,
        },
        generated_at=datetime.now(tz=timezone.utc),
    )


__all__ = ["build_webhook_payload", "find_subscribed_webhooks"]
