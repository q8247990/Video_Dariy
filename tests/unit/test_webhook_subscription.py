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


def test_webhook_subscribes_with_canonical_subscriptions_only() -> None:
    """Regression: subscription behavior relies solely on event_subscriptions_json.

    The legacy ``event_types_json`` field was removed; a webhook constructed
    without ``event_subscriptions_json`` MUST NOT receive any event.
    """

    hook = WebhookConfig(
        name="hook-canonical",
        url="https://example.com/webhook",
        event_subscriptions_json=[
            {"event": "daily_summary_generated", "version": "1.0"},
            {"event": "question_answered", "version": ""},
        ],
        enabled=True,
    )

    assert webhook_subscribes(hook, event_type="daily_summary_generated", version="1.0") is True
    assert webhook_subscribes(hook, event_type="question_answered", version="") is True
    assert webhook_subscribes(hook, event_type="scan_alert_state_changed", version="1.0") is False


def test_webhook_subscribes_does_not_fall_back_to_legacy_field() -> None:
    """A webhook whose only subscription data was the legacy list is empty.

    The legacy ``event_types_json`` column is gone; rows that still hold a
    stale value via direct attribute assignment (test-only escape hatch)
    must NOT cause the dispatcher to treat the hook as subscribed.
    """

    hook = WebhookConfig(
        name="hook-no-subs",
        url="https://example.com/webhook",
        enabled=True,
    )
    hook.event_subscriptions_json = None
    # Stale legacy payload lingering on the object — must be ignored.
    hook.event_types_json = ["daily_summary_generated"]  # type: ignore[attr-defined]

    assert webhook_subscribes(hook, event_type="daily_summary_generated", version="1.0") is False
