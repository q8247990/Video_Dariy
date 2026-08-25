from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.base_class import Base
from src.models.webhook_config import WebhookConfig
from src.models.webhook_delivery_log import WebhookDeliveryLog
from src.services.webhook_payload import build_webhook_event_payload
from src.tasks import webhook as webhook_task


def test_webhook_task_persists_independent_delivery_outcomes(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all(
        [
            WebhookConfig(
                name="success",
                url="https://success.example/hook",
                event_subscriptions_json=[{"event": "test_event", "version": "1.0"}],
                enabled=True,
            ),
            WebhookConfig(
                name="failure",
                url="https://failure.example/hook",
                event_subscriptions_json=[{"event": "test_event", "version": "1.0"}],
                enabled=True,
            ),
        ]
    )
    db.commit()

    def fake_deliver(*, db: Session, hook: WebhookConfig, **kwargs: object) -> WebhookDeliveryLog:
        status = "success" if hook.name == "success" else "failed"
        delivery = WebhookDeliveryLog(
            webhook_id=hook.id,
            event_type="test_event",
            status=status,
            attempt=1,
            error_message="unavailable" if status == "failed" else None,
        )
        db.add(delivery)
        db.flush()
        return delivery

    monkeypatch.setattr(webhook_task, "_deliver_hook", fake_deliver)
    successes, failures, delivery_ids = webhook_task._record_delivery_batch(
        db, "test_event", build_webhook_event_payload("test_event", {}), 1
    )

    assert {entry.status for entry in db.query(WebhookDeliveryLog).all()} == {"success", "failed"}
    assert (successes, failures, len(delivery_ids)) == (1, 1, 2)
    db.close()
