"""Regression tests for targeted (``webhook_id``-keyed) webhook delivery.

Locks the dual behavior of ``src.tasks.webhook.send_webhook_task``:
the canonical ``publish_daily_summary`` path enrolls one outbox row
per subscriber and the consumer-side task honors per-row
``webhook_id`` to deliver to that single hook; without ``webhook_id``
the task falls back to the legacy fan-out so producers that have not
yet been migrated keep working.

The producer-side (outbox enrollment) coverage lives in
``tests/unit/test_summary_publication.py`` and
``tests/integration/test_summary_publication_postgres.py``; the
end-to-end ``dispatch_webhook`` → outbox kwargs flow is pinned in
``tests/unit/test_outbox_cutover.py``. This file holds the
helper-level targeted-delivery tests, which run against the same
``pg_db`` fixture as ``test_webhook_delivery_task.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from src.models.webhook_config import WebhookConfig
from src.models.webhook_delivery_log import WebhookDeliveryLog
from src.services.webhook_payload import build_webhook_event_payload
from src.tasks import webhook as webhook_task


def _subscribe(db: Session, *, count: int) -> list[int]:
    """Insert N enabled hooks subscribing to the daily-summary event."""
    hooks = [
        WebhookConfig(
            name=f"hook-{i}",
            url=f"https://hook-{i}.example/hook",
            event_subscriptions_json=[{"event": "daily_summary_generated", "version": "1.0"}],
            enabled=True,
        )
        for i in range(1, count + 1)
    ]
    db.add_all(hooks)
    db.commit()
    return [int(h.id) for h in hooks]


def _record_hook(*, hook_id: int, status: str = "success") -> WebhookDeliveryLog:
    """Build a row that :func:`fake_deliver` would attach to the session."""
    return WebhookDeliveryLog(
        webhook_id=hook_id,
        event_type="daily_summary_generated",
        status=status,
        attempt=1,
    )


def test_send_webhook_task_n_subscribers_yield_n_targeted_deliveries(
    pg_db: Session, monkeypatch
) -> None:
    """Three subscribers: invoking the targeted helper once per id
    produces exactly three deliveries and zero fan-out duplicates.

    This is the direct regression for the duplicate-fanout bug: the
    canonical ``publish_daily_summary`` path now enrolls one outbox
    row per subscriber, and each row's consumer-side invocation
    targets exactly that single hook. End-to-end this means N
    subscribers → N deliveries (one each), never 2N.
    """
    hook_ids = _subscribe(pg_db, count=3)

    delivered: list[int] = []

    def fake_deliver(*, db: Session, hook: WebhookConfig, **kwargs: object) -> WebhookDeliveryLog:
        delivered.append(int(hook.id))
        delivery = _record_hook(hook_id=int(hook.id))
        db.add(delivery)
        db.flush()
        return delivery

    monkeypatch.setattr(webhook_task, "_deliver_hook", fake_deliver)
    payload = build_webhook_event_payload("daily_summary_generated", {})

    for hook_id in hook_ids:
        (
            successes,
            failures,
            delivery_ids,
            skip_reason,
        ) = webhook_task._deliver_to_hook(
            db=pg_db,
            hook_id=hook_id,
            event_type="daily_summary_generated",
            payload_version=str(payload["version"]),
            payload=payload,
            attempt=1,
        )
        assert skip_reason is None
        assert successes == 1
        assert failures == 0
        assert len(delivery_ids) == 1

    assert sorted(delivered) == sorted(hook_ids)
    assert pg_db.query(WebhookDeliveryLog).count() == len(hook_ids)


@pytest.mark.parametrize(
    "expected_reason",
    ["hook_not_found", "hook_disabled", "hook_not_subscribed"],
)
def test_send_webhook_task_targeted_skip_reasons(
    pg_db: Session,
    monkeypatch,
    expected_reason: str,
) -> None:
    """Validation rejects without retrying.

    With ``webhook_id`` set the targeted helper short-circuits on a
    missing / disabled / not-subscribed target and never attempts
    transport, recording a skip on the ``TaskLog`` instead of
    retrying.
    """
    if expected_reason == "hook_disabled":
        hook = WebhookConfig(
            name="disabled",
            url="https://disabled.example/hook",
            event_subscriptions_json=[{"event": "daily_summary_generated", "version": "1.0"}],
            enabled=False,
        )
        pg_db.add(hook)
        pg_db.commit()
        target_id = int(hook.id)
    elif expected_reason == "hook_not_subscribed":
        hook = WebhookConfig(
            name="non-subscriber",
            url="https://non-subscriber.example/hook",
            event_subscriptions_json=[{"event": "other_event", "version": "1.0"}],
            enabled=True,
        )
        pg_db.add(hook)
        pg_db.commit()
        target_id = int(hook.id)
    else:
        target_id = 99999

    delivery_attempted = False

    def fake_deliver(*, db: Session, hook: WebhookConfig, **kwargs: object) -> WebhookDeliveryLog:
        nonlocal delivery_attempted
        delivery_attempted = True
        delivery = _record_hook(hook_id=int(hook.id))
        db.add(delivery)
        db.flush()
        return delivery

    monkeypatch.setattr(webhook_task, "_deliver_hook", fake_deliver)
    payload = build_webhook_event_payload("daily_summary_generated", {})
    successes, failures, _ids, skip_reason = webhook_task._deliver_to_hook(
        db=pg_db,
        hook_id=target_id,
        event_type="daily_summary_generated",
        payload_version=str(payload["version"]),
        payload=payload,
        attempt=1,
    )

    assert delivery_attempted is False
    assert skip_reason == expected_reason
    assert successes == 0
    assert failures == 0
    assert pg_db.query(WebhookDeliveryLog).count() == 0


def test_send_webhook_task_targeted_recurring_loop_yields_exact_count(
    pg_db: Session, monkeypatch
) -> None:
    """``N subscribers`` × ``K rounds`` = ``N*K deliveries`` and no
    fan-out duplicates.

    Each iteration re-invokes the helper once per outbox row. The
    counter must reflect exactly ``N*K`` invocations of the
    underlying transport — proving the targeted helper does not
    regress to fan-out no matter how many rounds pass.
    """
    hook_ids = _subscribe(pg_db, count=4)
    rounds = 3

    delivered_total: dict[int, int] = dict.fromkeys(hook_ids, 0)

    def fake_deliver(*, db: Session, hook: WebhookConfig, **kwargs: object) -> WebhookDeliveryLog:
        delivered_total[int(hook.id)] += 1
        delivery = _record_hook(hook_id=int(hook.id))
        db.add(delivery)
        db.flush()
        return delivery

    monkeypatch.setattr(webhook_task, "_deliver_hook", fake_deliver)
    payload = build_webhook_event_payload("daily_summary_generated", {})

    for _ in range(rounds):
        for hook_id in hook_ids:
            (
                successes,
                failures,
                _ids,
                skip_reason,
            ) = webhook_task._deliver_to_hook(
                db=pg_db,
                hook_id=hook_id,
                event_type="daily_summary_generated",
                payload_version=str(payload["version"]),
                payload=payload,
                attempt=1,
            )
            assert skip_reason is None
            assert (successes, failures) == (1, 0)

    for _hook_id, count in delivered_total.items():
        assert count == rounds
    assert pg_db.query(WebhookDeliveryLog).count() == len(hook_ids) * rounds
