import logging

import httpx
from sqlalchemy.orm import Session

from src.core.celery_app import celery_app
from src.db.session import SessionLocal
from src.models.webhook_config import WebhookConfig
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.task_dispatch_control import bind_or_create_running_task_log, finalize_task_log
from src.services.webhook_payload import is_webhook_event_payload, to_json_safe
from src.services.webhook_subscription import webhook_subscribes

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def send_webhook_task(self, event_type: str, payload: dict) -> dict:
    db: Session = SessionLocal()
    if not is_webhook_event_payload(event_type, payload):
        raise ValueError(
            "invalid webhook payload envelope: require event/version/generated_at/data"
        )

    safe_payload = to_json_safe(payload)

    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    task_log = bind_or_create_running_task_log(
        db,
        queue_task_id=queue_task_id or None,
        task_type=TaskType.WEBHOOK_PUSH,
        task_target_id=None,
        detail_json={"event_type": event_type, "payload": safe_payload},
    )
    db.commit()

    try:
        # Find all enabled webhooks subscribed to this event_type
        webhooks = db.query(WebhookConfig).filter(WebhookConfig.enabled).all()
        payload_version = str(payload.get("version") or "")

        sent_count = 0
        for hook in webhooks:
            if not webhook_subscribes(hook, event_type=event_type, version=payload_version):
                continue

            headers = hook.headers_json or {}
            headers["Content-Type"] = "application/json"

            try:
                with httpx.Client(timeout=10) as client:
                    response = client.post(hook.url, headers=headers, json=safe_payload)
                    response.raise_for_status()
                    sent_count += 1
            except Exception as e:
                logger.error("Webhook %s failed: %s", hook.name, e)

        finalize_task_log(task_log, TaskStatus.SUCCESS, f"Sent to {sent_count} webhooks")
        db.commit()
        return {"sent": sent_count}

    except Exception as e:
        logger.exception("Failed to process webhooks")
        db.rollback()
        finalize_task_log(task_log, TaskStatus.FAILED, str(e))
        task_log.retry_count = self.request.retries
        db.commit()

        # Exponential backoff retry
        countdown = 5**self.request.retries
        raise self.retry(exc=e, countdown=countdown) from e
    finally:
        db.close()
