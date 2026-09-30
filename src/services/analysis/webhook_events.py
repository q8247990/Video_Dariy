"""Analysis-completion webhook events.

Turns the (previously no-op) "session analyzed" hook into durable
outbox deliveries: on a successful finalize, one ``TaskLog`` +
``OutboxEvent`` is enrolled per subscriber of the
``session_analyzed`` event. The publisher process then triggers the
existing targeted ``send_webhook_task`` — the same at-least-once path
used by ``daily_summary_generated``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from src.application.outbox.contracts import OutboxCommand
from src.application.outbox.enqueue import enqueue_command
from src.models.webhook_config import WebhookConfig
from src.services.analysis.constants import WEBHOOK_EVENT_SESSION_ANALYZED
from src.services.dispatch import create_pending_task_log
from src.services.pipeline_constants import TaskType
from src.services.webhook_payload import build_webhook_event_payload
from src.services.webhook_subscription import webhook_subscribes

WEBHOOK_TASK_NAME: str = "src.tasks.webhook.send_webhook_task"
DEFAULT_QUEUE: str = "celery"
SUBSCRIBED_VERSION: str = "1.0"


def find_session_analyzed_subscribers(db: Session) -> list[int]:
    """Return enabled webhook ids subscribed to ``session_analyzed``."""
    hooks = db.query(WebhookConfig).filter(WebhookConfig.enabled.is_(True)).all()
    return [
        int(hook.id)
        for hook in hooks
        if webhook_subscribes(
            hook,
            event_type=WEBHOOK_EVENT_SESSION_ANALYZED,
            version=SUBSCRIBED_VERSION,
        )
    ]


def enroll_session_analyzed_webhooks(
    db: Session,
    *,
    session_id: int,
    source_id: int,
    events_created: int,
    file_count: int,
    analysis_status: str,
    subscribers: list[int],
) -> list[UUID]:
    """Enroll one task log + outbox row per subscriber in the caller's tx."""
    enrolled: list[UUID] = []
    generated_at = datetime.now(timezone.utc)
    for webhook_id in subscribers:
        task_log, _created = create_pending_task_log(
            db,
            task_type=TaskType.WEBHOOK_PUSH,
            task_target_id=int(webhook_id),
            detail_json={
                "event_type": WEBHOOK_EVENT_SESSION_ANALYZED,
                "webhook_id": int(webhook_id),
                "session_id": session_id,
            },
        )
        payload: dict[str, Any] = build_webhook_event_payload(
            WEBHOOK_EVENT_SESSION_ANALYZED,
            {
                "session_id": session_id,
                "source_id": source_id,
                "events_created": events_created,
                "file_count": file_count,
                "analysis_status": analysis_status,
                "webhook_id": int(webhook_id),
            },
            generated_at=generated_at,
        )
        command = OutboxCommand(
            task_name=WEBHOOK_TASK_NAME,
            queue=DEFAULT_QUEUE,
            args=(),
            kwargs={
                "event_type": WEBHOOK_EVENT_SESSION_ANALYZED,
                "webhook_id": int(webhook_id),
                "payload": payload,
            },
        )
        outcome = enqueue_command(db, command, task_log)
        enrolled.append(outcome.event.event_id)
    return enrolled


__all__ = [
    "enroll_session_analyzed_webhooks",
    "find_session_analyzed_subscribers",
]
