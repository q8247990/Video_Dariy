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
    assert normalized["event_types_json"] == ["daily_summary_generated", "all"]


def test_normalize_webhook_payload_accepts_legacy_event_types() -> None:
    payload = {
        "event_types_json": ["daily_summary_generated", "question_answered", ""],
    }

    normalized = _normalize_webhook_payload(payload)

    assert normalized["event_subscriptions_json"] == [
        {"event": "daily_summary_generated", "version": ""},
        {"event": "question_answered", "version": ""},
    ]
    assert normalized["event_types_json"] == ["daily_summary_generated", "question_answered"]


def test_normalize_webhook_payload_defaults_to_empty() -> None:
    payload: dict = {}

    normalized = _normalize_webhook_payload(payload)

    assert normalized["event_subscriptions_json"] == []
    assert normalized["event_types_json"] == []
