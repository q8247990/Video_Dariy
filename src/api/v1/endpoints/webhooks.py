import logging
from typing import Any

from fastapi import APIRouter
from kombu.exceptions import OperationalError

from src.api.deps import DB, CurrentUser, Orchestrator
from src.application.pipeline.commands import SendWebhookCommand
from src.models.webhook_config import WebhookConfig
from src.schemas.response import BaseResponse, PaginatedData, PaginatedResponse, PaginationDetails
from src.schemas.webhook import WebhookCreate, WebhookResponse, WebhookUpdate
from src.services.webhook_payload import build_webhook_event_payload


def _normalize_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    subscriptions = payload.get("event_subscriptions_json")
    if subscriptions is None:
        legacy_events = payload.get("event_types_json")
        if isinstance(legacy_events, list):
            subscriptions = [
                {"event": str(event_name).strip(), "version": ""}
                for event_name in legacy_events
                if str(event_name or "").strip()
            ]
        else:
            subscriptions = []

    normalized_subscriptions: list[dict[str, str]] = []
    for item in subscriptions:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip()
        version = str(item.get("version") or "").strip()
        if not event:
            continue
        normalized_subscriptions.append({"event": event, "version": version})

    payload["event_subscriptions_json"] = normalized_subscriptions
    payload["event_types_json"] = [item["event"] for item in normalized_subscriptions]
    return payload


router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("", response_model=PaginatedResponse[WebhookResponse])
def get_webhooks(db: DB, current_user: CurrentUser, page: int = 1, page_size: int = 20) -> Any:
    query = db.query(WebhookConfig).order_by(WebhookConfig.id.desc())
    total = query.count()
    hooks = query.offset((page - 1) * page_size).limit(page_size).all()

    return PaginatedResponse(
        data=PaginatedData(
            list=[WebhookResponse.model_validate(h) for h in hooks],
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )


@router.post("", response_model=BaseResponse[WebhookResponse])
def create_webhook(db: DB, current_user: CurrentUser, data: WebhookCreate) -> Any:
    payload = _normalize_webhook_payload(data.model_dump())
    hook = WebhookConfig(**payload)
    db.add(hook)
    db.commit()
    db.refresh(hook)
    return BaseResponse(data=WebhookResponse.model_validate(hook))


@router.put("/{id}", response_model=BaseResponse[WebhookResponse])
def update_webhook(db: DB, current_user: CurrentUser, id: int, data: WebhookUpdate) -> Any:
    hook = db.query(WebhookConfig).filter(WebhookConfig.id == id).first()
    if not hook:
        return BaseResponse(code=4002, message="Webhook not found")

    updates = _normalize_webhook_payload(data.model_dump(exclude_unset=True))
    for key, value in updates.items():
        setattr(hook, key, value)

    db.commit()
    db.refresh(hook)
    return BaseResponse(data=WebhookResponse.model_validate(hook))


@router.delete("/{id}", response_model=BaseResponse[dict])
def delete_webhook(db: DB, current_user: CurrentUser, id: int) -> Any:
    hook = db.query(WebhookConfig).filter(WebhookConfig.id == id).first()
    if hook:
        db.delete(hook)
        db.commit()
    return BaseResponse(data={})


@router.post("/{id}/test", response_model=BaseResponse[dict])
def test_webhook(db: DB, current_user: CurrentUser, orchestrator: Orchestrator, id: int) -> Any:
    hook = db.query(WebhookConfig).filter(WebhookConfig.id == id).first()
    if not hook:
        return BaseResponse(code=4002, message="Webhook not found")

    payload = build_webhook_event_payload(
        "test_event",
        {"message": "This is a test webhook push."},
    )

    try:
        orchestrator.dispatch_webhook(SendWebhookCommand(event_type="test_event", payload=payload))
    except OperationalError as e:
        logger.exception("Failed to enqueue test webhook task for webhook_id=%s", id)
        return BaseResponse(code=5001, message=f"Task queue unavailable: {e}")
    return BaseResponse(data={"success": True, "message": "Test webhook scheduled."})
