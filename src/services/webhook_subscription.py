from typing import Any

from src.models.webhook_config import WebhookConfig


def _normalize_subscription_item(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    event = str(item.get("event") or "").strip()
    version = str(item.get("version") or "").strip()
    if not event:
        return None
    return {
        "event": event,
        "version": version,
    }


def _extract_subscriptions(hook: WebhookConfig) -> list[dict[str, str]]:
    subscriptions: list[dict[str, str]] = []

    rules = hook.event_subscriptions_json
    if isinstance(rules, list):
        for item in rules:
            normalized = _normalize_subscription_item(item)
            if normalized is not None:
                subscriptions.append(normalized)

    if subscriptions:
        return subscriptions

    legacy_events = hook.event_types_json
    if isinstance(legacy_events, list):
        for event_name in legacy_events:
            event = str(event_name or "").strip()
            if not event:
                continue
            subscriptions.append({"event": event, "version": ""})

    return subscriptions


def webhook_subscribes(hook: WebhookConfig, *, event_type: str, version: str) -> bool:
    subscriptions = _extract_subscriptions(hook)
    if not subscriptions:
        return False

    for rule in subscriptions:
        event_rule = rule["event"]
        version_rule = rule["version"]

        event_match = event_rule in {"all", "*", event_type}
        version_match = not version_rule or version_rule in {"*", version}

        if event_match and version_match:
            return True

    return False
