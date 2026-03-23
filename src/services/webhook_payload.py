from datetime import date, datetime
from typing import Any, Optional


def to_json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [to_json_safe(item) for item in value]
    if isinstance(value, set):
        return [to_json_safe(item) for item in sorted(value, key=lambda x: str(x))]
    return value


def build_webhook_event_payload(
    event_type: str,
    data: dict[str, Any],
    *,
    version: str = "1.0",
    generated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    ts = generated_at or datetime.utcnow()
    return {
        "event": event_type,
        "version": version,
        "generated_at": ts.isoformat(),
        "data": to_json_safe(data),
    }


def is_webhook_event_payload(event_type: str, payload: dict[str, Any]) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("event") == event_type
        and "data" in payload
        and "generated_at" in payload
        and "version" in payload
    )
