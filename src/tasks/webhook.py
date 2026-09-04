import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

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
    db: Session,
    hook: WebhookConfig,
    event_type: str,
    payload: dict[str, Any],
    attempt: int,
) -> WebhookDeliveryLog:
    headers = dict(hook.headers_json or {})
    headers["Content-Type"] = "application/json"
    delivery = WebhookDeliveryLog(
        webhook_id=hook.id, event_type=event_type, status="running", attempt=attempt
    )
    db.add(delivery)
    db.flush()
    response: httpx.Response | None = None
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
        error_response: httpx.Response | None = getattr(error, "response", None)
        delivery.status_code = error_response.status_code if error_response is not None else None
        logger.warning("Webhook delivery failed webhook_id=%s: %s", hook.id, error)
    return delivery


def _record_delivery_batch(
    db: Session, event_type: str, payload: dict[str, Any], attempt: int
) -> tuple[int, int, list[int]]:
    """Legacy fan-out used when ``webhook_id`` is absent from kwargs."""
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


def _deliver_to_hook(
    *,
    db: Session,
    hook_id: int,
    event_type: str,
    payload_version: str,
    payload: dict[str, Any],
    attempt: int,
) -> tuple[int, int, list[int], Optional[str]]:
    """Deliver to the targeted ``hook_id`` only.

    Returns ``(successes, failures, delivery_ids, skip_reason)``.
    ``skip_reason`` is non-None when the target failed validation
    (missing / disabled / not subscribed) so the caller records a
    skip on the ``TaskLog`` instead of retrying.
    """
    hook = db.query(WebhookConfig).filter(WebhookConfig.id == hook_id).first()
    if hook is None:
        return 0, 0, [], "hook_not_found"
    if not hook.enabled:
        return 0, 0, [], "hook_disabled"
    if not webhook_subscribes(hook, event_type=event_type, version=payload_version):
        return 0, 0, [], "hook_not_subscribed"

    delivery = _deliver_hook(
        db=db,
        hook=hook,
        event_type=event_type,
        payload=payload,
        attempt=attempt,
    )
    successes = 1 if delivery.status == "success" else 0
    return successes, 1 - successes, [delivery.id], None


@celery_app.task(bind=True, max_retries=3)  # type: ignore[untyped-decorator]
def send_webhook_task(
    self: Any,
    event_type: str,
    payload: dict[str, Any],
    webhook_id: Optional[int] = None,
) -> dict[str, Any]:
    """Deliver one webhook event.

    With ``webhook_id`` set delivers to that single subscriber only;
    ``None`` falls back to the legacy fan-out so producers that have
    not been migrated keep working.
    """
    with task_db_session() as db:
        if not is_webhook_event_payload(event_type, payload):
            raise ValueError(
                "invalid webhook payload envelope: require event/version/generated_at/data"
            )
        safe_payload = to_json_safe(payload)
        queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
        detail_json: dict[str, Any] = {"event_type": event_type, "payload": safe_payload}
        if webhook_id is not None:
            detail_json["webhook_id"] = int(webhook_id)
        task_log = bind_or_create_running_task_log(
            db,
            queue_task_id=queue_task_id or None,
            task_type=TaskType.WEBHOOK_PUSH,
            task_target_id=int(webhook_id) if webhook_id is not None else None,
            detail_json=detail_json,
        )
        if task_log is None:
            logger.warning(
                "Stale webhook message %s for event %s; task log already finalized, skipping",
                queue_task_id,
                event_type,
            )
            return {"skipped": True, "reason": "stale_message"}
        db.commit()

        if webhook_id is None:
            successes, failures, delivery_ids = _record_delivery_batch(
                db, event_type, safe_payload, self.request.retries + 1
            )
            skip_reason: Optional[str] = None
        else:
            payload_version = str(safe_payload.get("version") or "")
            successes, failures, delivery_ids, skip_reason = _deliver_to_hook(
                db=db,
                hook_id=int(webhook_id),
                event_type=event_type,
                payload_version=payload_version,
                payload=safe_payload,
                attempt=self.request.retries + 1,
            )

        detail = dict(task_log.detail_json or {})
        detail["delivery_successes"] = successes
        detail["delivery_failures"] = failures
        detail["delivery_ids"] = delivery_ids
        if skip_reason is not None:
            detail["skip_reason"] = skip_reason
        task_log.detail_json = detail

        if skip_reason is not None:
            finalize_task_log(
                task_log,
                TaskStatus.SUCCESS,
                f"Webhook delivery skipped: {skip_reason} (webhook_id={webhook_id})",
            )
            task_log.retry_count = self.request.retries
            db.commit()
            return {"sent": 0, "failed": 0, "skipped": True, "reason": skip_reason}

        final_status = TaskStatus.SUCCESS if failures == 0 else TaskStatus.FAILED
        finalize_task_log(
            task_log, final_status, f"Webhook deliveries: {successes} succeeded, {failures} failed"
        )
        task_log.retry_count = self.request.retries
        db.commit()
        if failures:
            raise self.retry(countdown=5**self.request.retries)
        return {"sent": successes, "failed": failures}
