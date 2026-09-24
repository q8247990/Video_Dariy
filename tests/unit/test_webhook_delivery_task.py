from collections.abc import Iterator
from contextlib import contextmanager

import httpx as httpx_module
import pytest
from sqlalchemy.orm import Session

from src.models.webhook_config import WebhookConfig
from src.models.webhook_delivery_log import WebhookDeliveryLog
from src.services.webhook_payload import build_webhook_event_payload
from src.tasks import webhook as webhook_task
from src.tasks.webhook import send_webhook_task


class _StubResponse:
    """Stand-in for :class:`httpx.Response` that always looks like success."""

    status_code = 200
    is_redirect = False

    def raise_for_status(self) -> None:
        return None


class _StubRetry(Exception):
    """Sentinel for the patched ``send_webhook_task.retry`` to short-circuit Celery retries."""


class _RoutingClient:
    """httpx.Client double: routes the success / failure URLs to different responses.

    The :class:`WebhookConfig` rows in this test carry distinguishable
    paths under a resolvable public IP (so :func:`validate_webhook_url`
    passes); the routing client returns 200 for the ``/success`` path
    and raises an :class:`httpx.HTTPError` for the ``/failure`` path.
    This drives :func:`src.tasks.webhook._deliver_hook` through its
    real transport boundary without monkey-patching any internal
    symbol.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str | None]]] = []

    def post(self, url: str, headers: dict[str, str | None], json: dict) -> _StubResponse:
        del json
        self.calls.append((url, headers))
        if "/failure" in url:
            raise httpx_module.ConnectError("connection refused")
        return _StubResponse()

    def close(self) -> None:
        return None

    def __enter__(self) -> "_RoutingClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_webhook_task_persists_independent_delivery_outcomes(
    pg_db_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``send_webhook_task(webhook_id=None)`` fans out to every subscriber, persisting
    one ``WebhookDeliveryLog`` per hook with the correct success/failed outcome.

    Drives the public Celery task entry; ``task_db_session`` is routed through
    the ``pg_db_factory`` so the seeded ``WebhookConfig`` rows (committed on a
    real-schema connection) are visible from inside the task body.
    """
    session_factory = pg_db_factory
    seed_db: Session = session_factory()
    try:
        seed_db.add_all(
            [
                WebhookConfig(
                    name="success",
                    url="https://1.1.1.1:443/success-hook",
                    event_subscriptions_json=[{"event": "test_event", "version": "1.0"}],
                    enabled=True,
                ),
                WebhookConfig(
                    name="failure",
                    url="https://1.1.1.1:443/failure-hook",
                    event_subscriptions_json=[{"event": "test_event", "version": "1.0"}],
                    enabled=True,
                ),
            ]
        )
        seed_db.commit()
    finally:
        seed_db.close()

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(webhook_task, "task_db_session", _task_session)

    routing_client = _RoutingClient()
    monkeypatch.setattr("src.tasks.webhook.httpx.Client", lambda *a, **kw: routing_client)

    def _no_retry(*_args: object, **_kwargs: object) -> None:
        raise _StubRetry()

    monkeypatch.setattr(send_webhook_task, "retry", _no_retry, raising=False)

    payload = build_webhook_event_payload("test_event", {})
    try:
        send_webhook_task.run(event_type="test_event", payload=payload, webhook_id=None)
    except _StubRetry:
        pass

    verify_db: Session = session_factory()
    try:
        deliveries = verify_db.query(WebhookDeliveryLog).all()
        hook_names_by_id = {row.id: row.name for row in verify_db.query(WebhookConfig).all()}
        assert {entry.status for entry in deliveries} == {"success", "failed"}
        assert {hook_names_by_id[entry.webhook_id] for entry in deliveries} == {
            "success",
            "failure",
        }
    finally:
        verify_db.close()

    assert len(routing_client.calls) == 2
    assert {url for url, _ in routing_client.calls} == {
        "https://1.1.1.1:443/success-hook",
        "https://1.1.1.1:443/failure-hook",
    }
