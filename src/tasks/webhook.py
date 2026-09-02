import logging
from datetime import datetime, timezone

import httpx

from src.core.celery_app import celery_app
from src.db.session import task_db_session
from src.models.webhook_config import WebhookConfig
from src.models.webhook_delivery_log import WebhookDeliveryLog
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.task_dispatch_control import bind_or_create_running_task_log, finalize_task_log
from src.services.webhook_payload import is_webhook_event_payload, to_json_safe
from src.services.webhook_subscription import webhook_subscribes
from src.services.webhook_url_policy import WebhookUrlPolicyError, validate_webhook_url

logger = logging.getLogger(__name__)


def _deliver_hook(
    *,
    db,
    hook: WebhookConfig,
    event_type: str,
    payload: dict,
    attempt: int,
) -> WebhookDeliveryLog:
    headers = dict(hook.headers_json or {})
    headers["Content-Type"] = "application/json"
    delivery = WebhookDeliveryLog(
        webhook_id=hook.id, event_type=event_type, status="running", attempt=attempt
    )
    db.add(delivery)
    db.flush()
    try:
        validate_webhook_url(hook.url)
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(hook.url, headers=headers, json=payload)
            if response.is_redirect:
                raise WebhookUrlPolicyError("webhook redirects are not permitted")
            response.raise_for_status()
        delivery.status = "success"
        delivery.status_code = response.status_code
        delivery.delivered_at = datetime.now(timezone.utc)
    except (WebhookUrlPolicyError, httpx.HTTPError) as error:
        delivery.status = "failed"
        delivery.error_message = str(error)[:1024]
        response = getattr(error, "response", None)
        delivery.status_code = response.status_code if response is not None else None
        logger.warning("Webhook delivery failed webhook_id=%s: %s", hook.id, error)
    return delivery


def _record_delivery_batch(
    db, event_type: str, payload: dict, attempt: int
) -> tuple[int, int, list[int]]:
    webhooks = db.query(WebhookConfig).filter(WebhookConfig.enabled).all()
    payload_version = str(payload.get("version") or "")
    deliveries = [
        _deliver_hook(
            db=db,
            hook=hook,
            event_type=event_type,
            payload=payload,
            attempt=attempt,
        )
        for hook in webhooks
        if webhook_subscribes(hook, event_type=event_type, version=payload_version)
    ]
    successes = sum(delivery.status == "success" for delivery in deliveries)
    return successes, len(deliveries) - successes, [delivery.id for delivery in deliveries]


@celery_app.task(bind=True, max_retries=3)
def send_webhook_task(self, event_type: str, payload: dict) -> dict:
    with task_db_session() as db:
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
        if task_log is None:
            logger.warning(
                "Stale webhook message %s for event %s; task log already finalized, skipping",
                queue_task_id,
                event_type,
            )
            return {"skipped": True, "reason": "stale_message"}
        db.commit()
        successes, failures, delivery_ids = _record_delivery_batch(
            db, event_type, safe_payload, self.request.retries + 1
        )
        detail = dict(task_log.detail_json or {})
        detail["delivery_successes"] = successes
        detail["delivery_failures"] = failures
        detail["delivery_ids"] = delivery_ids
        task_log.detail_json = detail
        final_status = TaskStatus.SUCCESS if failures == 0 else TaskStatus.FAILED
        finalize_task_log(
            task_log, final_status, f"Webhook deliveries: {successes} succeeded, {failures} failed"
        )
        task_log.retry_count = self.request.retries
        db.commit()
        if failures:
            raise self.retry(countdown=5**self.request.retries)
        return {"sent": successes, "failed": failures}
