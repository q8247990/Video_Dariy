"""Final-stage helper — webhook discovery.

The seam between the per-day summary generation
(``generation`` / ``normalization``) and the publication concerns
the Celery task owns (publish-daily-summary use case).

The legacy :func:`build_webhook_payload` fan-out helper was removed
once the canonical ``publish_daily_summary`` path took over webhook
delivery: the publish use case now enrolls one outbox row per
subscriber so the publisher triggers exactly N targeted deliveries
instead of one duplicate fan-out. The webhook envelope itself still
flows through :mod:`src.services.webhook_payload`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.models.webhook_config import WebhookConfig
from src.services.summarizer.constants import WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED
from src.services.webhook_subscription import webhook_subscribes


def find_subscribed_webhooks(db: Session) -> list[int]:
    """Return the ids of enabled ``WebhookConfig`` rows that subscribe to the event."""
    hooks = db.query(WebhookConfig).filter(WebhookConfig.enabled.is_(True)).all()
    return [
        int(hook.id)
        for hook in hooks
        if webhook_subscribes(hook, event_type=WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED, version="1.0")
    ]


__all__ = ["find_subscribed_webhooks"]
