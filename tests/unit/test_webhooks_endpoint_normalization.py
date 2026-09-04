from src.api.v1.endpoints.webhooks import _normalize_webhook_payload


def test_normalize_webhook_payload_prefers_event_subscriptions() -> None:
    payload = {
        "event_subscriptions_json": [
            {"event": "daily_summary_generated", "version": "1.0"},
            {"event": "", "version": "1.0"},
            {"event": "all", "version": ""},
        ]
    }

    normalized = _normalize_webhook_payload(payload)

    assert normalized["event_subscriptions_json"] == [
        {"event": "daily_summary_generated", "version": "1.0"},
        {"event": "all", "version": ""},
    ]
    assert "event_types_json" not in normalized


def test_normalize_webhook_payload_ignores_unknown_keys() -> None:
    payload = {
        "event_subscriptions_json": [
            {"event": "daily_summary_generated", "version": "1.0"},
        ],
        "event_types_json": ["should_be_discarded"],
    }

    normalized = _normalize_webhook_payload(payload)

    assert normalized["event_subscriptions_json"] == [
        {"event": "daily_summary_generated", "version": "1.0"},
    ]
    assert "event_types_json" not in normalized


def test_normalize_webhook_payload_defaults_to_empty() -> None:
    payload: dict = {}

    normalized = _normalize_webhook_payload(payload)

    assert normalized["event_subscriptions_json"] == []
    assert "event_types_json" not in normalized
