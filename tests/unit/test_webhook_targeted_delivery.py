"""Regression tests for targeted (``webhook_id``-keyed) webhook delivery.

Drives :func:`src.tasks.webhook.send_webhook_task` end-to-end against
real PostgreSQL (the ``pg_db_factory`` fixture): the canonical
``publish_daily_summary`` path enrolls one outbox row per subscriber
and the consumer-side task honours per-row ``webhook_id`` to deliver
to that single hook; without ``webhook_id`` the task falls back to the
legacy fan-out so producers that have not been migrated keep working.

The producer-side (outbox enrollment) coverage lives in
``tests/unit/test_summary_publication.py`` and
``tests/integration/test_summary_publication_postgres.py``; the
end-to-end ``dispatch_webhook`` → outbox kwargs flow is pinned in
``tests/unit/test_outbox_cutover.py``. This file holds the
delivery-targeting regression tests, which assert only what the public
entry returns / records (return dict, ``WebhookDeliveryLog`` rows,
``TaskLog`` finalization).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.models.webhook_config import WebhookConfig
from src.models.webhook_delivery_log import WebhookDeliveryLog
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.webhook_payload import build_webhook_event_payload
from src.tasks import webhook as webhook_task
from src.tasks.webhook import send_webhook_task


class _StubResponse:
    """httpx.Response double: always succeeds with a 200 OK body."""

    status_code = 200
    is_redirect = False

    def raise_for_status(self) -> None:
        return None


class _OkClient:
    """httpx.Client double used by all targeted-delivery tests.

    Every outbound POST returns a successful response so the targeted
    delivery path lands a ``WebhookDeliveryLog`` with
    ``status="success"``. The double records every call so the recurring
    loop test can assert the exact call count.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, url: str, headers: dict[str, str | None], json: dict) -> _StubResponse:
        del headers, json
        self.calls.append(url)
        return _StubResponse()

    def close(self) -> None:
        return None

    def __enter__(self) -> "_OkClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@pytest.fixture(autouse=True)
def _bind_routing_client(monkeypatch: pytest.MonkeyPatch) -> _OkClient:
    """Default the httpx transport for every test in this module.

    Tests that need a different behaviour replace :attr:`client` on the
    returned object (the monkeypatched factory pulls ``client`` off the
    closure each time).
    """

    def _factory(_client: _OkClient):

        def _build(*_a: object, **_kw: object) -> _OkClient:
            return _client

        return _build

    client = _OkClient()
    monkeypatch.setattr("src.tasks.webhook.httpx.Client", _factory(client))
    return client


@pytest.fixture(autouse=True)
def _route_task_session(monkeypatch: pytest.MonkeyPatch, pg_db_factory):
    """Route the task's ``task_db_session`` through the real-schema factory."""

    session_factory = pg_db_factory

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(webhook_task, "task_db_session", _task_session)


def _subscribe(
    db: Session, *, count: int, event_type: str = "daily_summary_generated"
) -> list[int]:
    """Insert N enabled hooks subscribing to ``event_type``."""

    hooks = [
        WebhookConfig(
            name=f"hook-{i}",
            url=f"https://1.1.1.1:443/targeted/{event_type}/hook-{i}",
            event_subscriptions_json=[{"event": event_type, "version": "1.0"}],
            enabled=True,
        )
        for i in range(1, count + 1)
    ]
    db.add_all(hooks)
    db.commit()
    return [int(h.id) for h in hooks]


def _drive_targeted(
    session_factory, hook_id: int, event_type: str = "daily_summary_generated"
) -> dict:
    """Invoke the public task body once for a single ``hook_id``."""

    payload = build_webhook_event_payload(event_type, {})
    return send_webhook_task.run(event_type=event_type, payload=payload, webhook_id=hook_id)


def test_send_webhook_task_n_subscribers_yield_n_targeted_deliveries(
    pg_db_factory, _bind_routing_client: _OkClient
) -> None:
    """N subscribers × 1 invocation per ``webhook_id`` → exactly N deliveries.

    Direct regression for the duplicate-fanout bug: each enrolled
    outbox row targets exactly one hook, so the consumer-side
    invocation produces one ``WebhookDeliveryLog`` per call and the
    total across N subscribers is N — never 2N.
    """
    session_factory = pg_db_factory
    seed_db: Session = session_factory()
    try:
        hook_ids = _subscribe(seed_db, count=3)
    finally:
        seed_db.close()

    for hook_id in hook_ids:
        result = _drive_targeted(session_factory, hook_id)
        assert result == {"sent": 1, "failed": 0}

    verify_db: Session = session_factory()
    try:
        deliveries = verify_db.query(WebhookDeliveryLog).all()
        assert len(deliveries) == 3
        assert {d.webhook_id for d in deliveries} == set(hook_ids)
        assert {d.status for d in deliveries} == {"success"}
        task_logs = (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_type == TaskType.WEBHOOK_PUSH)
            .all()
        )
        assert len(task_logs) == 3
        assert all(log.status == TaskStatus.SUCCESS for log in task_logs)
        assert all(log.detail_json.get("skip_reason") is None for log in task_logs)
    finally:
        verify_db.close()


@pytest.mark.parametrize(
    "expected_reason",
    ["hook_not_found", "hook_disabled", "hook_not_subscribed"],
)
def test_send_webhook_task_targeted_skip_reasons(
    pg_db_factory,
    _bind_routing_client: _OkClient,
    expected_reason: str,
) -> None:
    """Validation rejects without retrying.

    The targeted public entry records the skip reason on the
    ``TaskLog`` (``detail_json["skip_reason"]``) and finalises the row
    as ``SUCCESS`` so the outbox publisher does not retry. No outbound
    HTTP call is made for any of the three rejection paths.
    """
    session_factory = pg_db_factory
    seed_db: Session = session_factory()
    try:
        if expected_reason == "hook_disabled":
            hook = WebhookConfig(
                name="disabled",
                url="https://1.1.1.1:443/disabled",
                event_subscriptions_json=[{"event": "daily_summary_generated", "version": "1.0"}],
                enabled=False,
            )
            seed_db.add(hook)
            seed_db.commit()
            target_id = int(hook.id)
        elif expected_reason == "hook_not_subscribed":
            hook = WebhookConfig(
                name="non-subscriber",
                url="https://1.1.1.1:443/non-subscriber",
                event_subscriptions_json=[{"event": "other_event", "version": "1.0"}],
                enabled=True,
            )
            seed_db.add(hook)
            seed_db.commit()
            target_id = int(hook.id)
        else:
            target_id = 99999
    finally:
        seed_db.close()

    result = _drive_targeted(session_factory, target_id)

    assert result == {"sent": 0, "failed": 0, "skipped": True, "reason": expected_reason}

    verify_db: Session = session_factory()
    try:
        deliveries = verify_db.query(WebhookDeliveryLog).all()
        assert len(deliveries) == 0
        task_log = (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_type == TaskType.WEBHOOK_PUSH)
            .order_by(TaskLog.id.desc())
            .first()
        )
        assert task_log is not None
        assert task_log.status == TaskStatus.SUCCESS
        assert task_log.detail_json["skip_reason"] == expected_reason
        assert task_log.detail_json["delivery_successes"] == 0
        assert task_log.detail_json["delivery_failures"] == 0
    finally:
        verify_db.close()


def test_send_webhook_task_targeted_recurring_loop_yields_exact_count(
    pg_db_factory, _bind_routing_client: _OkClient
) -> None:
    """``N subscribers`` × ``K rounds`` = ``N*K deliveries`` and no fan-out duplicates.

    Each iteration invokes the public entry once per outbox row. The
    routing client must see exactly ``N*K`` POST calls — proving the
    targeted entry never fans out to other subscribers no matter how
    many rounds pass.
    """
    session_factory = pg_db_factory
    seed_db: Session = session_factory()
    try:
        hook_ids = _subscribe(seed_db, count=4)
    finally:
        seed_db.close()
    rounds = 3

    for _ in range(rounds):
        for hook_id in hook_ids:
            result = _drive_targeted(session_factory, hook_id)
            assert result == {"sent": 1, "failed": 0}

    verify_db: Session = session_factory()
    try:
        deliveries = verify_db.query(WebhookDeliveryLog).all()
        assert len(deliveries) == len(hook_ids) * rounds
    finally:
        verify_db.close()

    assert len(_bind_routing_client.calls) == len(hook_ids) * rounds
