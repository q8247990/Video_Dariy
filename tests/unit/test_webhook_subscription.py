from src.models.webhook_config import WebhookConfig
from src.services.webhook_subscription import webhook_subscribes


def test_webhook_subscribes_with_new_subscription_rules() -> None:
    hook = WebhookConfig(
        name="hook-1",
        url="https://example.com/webhook",
        event_subscriptions_json=[
            {"event": "daily_summary_generated", "version": "1.0"},
        ],
        enabled=True,
    )

    assert webhook_subscribes(hook, event_type="daily_summary_generated", version="1.0") is True
    assert webhook_subscribes(hook, event_type="daily_summary_generated", version="2.0") is False


def test_webhook_subscribes_with_legacy_event_types() -> None:
    hook = WebhookConfig(
        name="hook-legacy",
        url="https://example.com/webhook",
        event_types_json=["daily_summary_generated", "question_answered"],
        enabled=True,
    )

    assert webhook_subscribes(hook, event_type="daily_summary_generated", version="1.0") is True
    assert webhook_subscribes(hook, event_type="question_answered", version="") is True
    assert webhook_subscribes(hook, event_type="scan_alert_state_changed", version="1.0") is False
