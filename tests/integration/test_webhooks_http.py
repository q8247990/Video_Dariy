"""Real-HTTP contract tests for the webhooks endpoints.

Covers CRUD on ``WebhookConfig`` plus the ``POST /{id}/test`` dispatch
endpoint. The previous unit test suite
(``tests/unit/test_webhooks_endpoint_normalization.py``) only exercised
the private ``_normalize_webhook_payload`` helper — its core coverage
(the legacy ``event_types_json`` field being silently dropped) is now
expressed through HTTP: the request body is allowed to carry
``event_types_json``, and the response payload (and any subsequent
``GET /api/v1/webhooks`` row) only reflects ``event_subscriptions_json``.

These tests:

* drive a real FastAPI ``TestClient`` over the ``webhooks`` router via
  :func:`tests.integration.conftest.make_http_client`;
* verify the HTTP status code, the ``code``/``message``/``data``
  envelope, and the persisted fields;
* for ``POST /{id}/test`` substitute the API composition root's
  dispatcher with a :class:`FakeTaskDispatcher` (port-level fake) so
  the dispatched ``SendWebhookCommand`` is asserted by command object
  rather than a mock's call history;
* for URL-policy failures (localhost / private IPs) exercise the real
  ``validate_webhook_url`` policy through the HTTP layer — the DNS
  resolution path uses literal IPs that resolve locally.

SIZE_OK: the task brief explicitly mandates a single file for the
``webhooks`` endpoint family (``tests/integration/test_webhooks_http.py``),
which naturally pushes the file past 250 pure LOC once every CRUD
verb (GET / POST / PUT / DELETE / POST-test) gets its own self-contained
HTTP contract test with the persisted-row, 401, and one business-error
path each. Splitting per-verb would break the explicit per-endpoint-file
layout.
"""

from __future__ import annotations

import socket
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.api import deps as api_deps
from src.api.error_status import ResponseStatusMiddleware
from src.api.v1.endpoints import webhooks as webhooks_module
from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeTaskDispatcher
from src.application.pipeline.commands import SendWebhookCommand
from src.models.webhook_config import WebhookConfig

# ---------------------------------------------------------------------------
# DNS shim: the SSRF policy resolves hostnames via socket.getaddrinfo. For
# tests that need a "public" target we monkeypatch DNS to a known-public
# IPv4 so we never touch the network; for prohibited targets we use literal
# loopback / RFC-1918 IPs that resolve locally and are rejected by the
# policy itself.
# ---------------------------------------------------------------------------


def _stub_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, 1, 6, "", ("8.8.8.8", 0))],
    )


def _seed_webhook(
    pg_db: Session,
    *,
    name: str = "hook",
    url: str = "https://example.com/hook",
    enabled: bool = True,
    event_subscriptions_json: list[dict[str, str]] | None = None,
) -> WebhookConfig:
    hook = WebhookConfig(
        name=name,
        url=url,
        headers_json=None,
        event_subscriptions_json=event_subscriptions_json,
        enabled=enabled,
    )
    pg_db.add(hook)
    pg_db.commit()
    pg_db.refresh(hook)
    return hook


@pytest.fixture
def fake_dispatcher() -> Iterator[FakeTaskDispatcher]:
    """Bind a FakeTaskDispatcher to the API composition root.

    The webhook ``POST /{id}/test`` endpoint dispatches through the
    FastAPI ``Orchestrator`` dependency wired by
    :func:`src.api.deps.get_pipeline_orchestrator`, which in turn reads
    ``container.dispatcher``. Swapping the container here is enough to
    redirect dispatch into the port fake.

    Teardown restores the original container so subsequent tests are
    not contaminated.
    """

    original = api_deps._container
    fake = FakeTaskDispatcher()
    api_deps.set_container_for_tests(bootstrap_for_tests(dispatcher=fake))
    try:
        yield fake
    finally:
        api_deps.set_container_for_tests(original)


@pytest.fixture
def client(
    pg_db: Session, make_http_client, fake_dispatcher: FakeTaskDispatcher
) -> Iterator[TestClient]:
    with make_http_client(
        [(webhooks_module.router, "/api/v1/webhooks")],
        middleware=[ResponseStatusMiddleware],
    ) as test_client:
        yield test_client


@pytest.fixture
def anonymous_client(pg_db: Session, make_http_client) -> Iterator[TestClient]:
    with make_http_client(
        [(webhooks_module.router, "/api/v1/webhooks")],
        authenticated=False,
    ) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks — list
# ---------------------------------------------------------------------------


def test_list_webhooks_returns_paginated_envelope(client: TestClient, pg_db: Session) -> None:
    _seed_webhook(pg_db, name="hook-a", url="https://a.example/hook")
    _seed_webhook(pg_db, name="hook-b", url="https://b.example/hook")

    response = client.get("/api/v1/webhooks")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["pagination"] == {
        "page": 1,
        "page_size": 20,
        "total": 2,
    }
    listed = payload["data"]["list"]
    assert len(listed) == 2
    assert {item["name"] for item in listed} == {"hook-a", "hook-b"}
    # Legacy event_types_json must never appear in serialized payloads.
    for item in listed:
        assert "event_types_json" not in item


def test_list_webhooks_returns_401_when_unauthenticated(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.get("/api/v1/webhooks")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks — create
# ---------------------------------------------------------------------------


def test_create_webhook_persists_subscriptions_and_drops_legacy_field(
    client: TestClient, pg_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_public_dns(monkeypatch)

    response = client.post(
        "/api/v1/webhooks",
        json={
            "name": "daily-hook",
            "url": "https://public.example/hook",
            "event_subscriptions_json": [
                {"event": "daily_summary_generated", "version": "1.0"},
            ],
            # Legacy field: must be silently dropped by the schema layer.
            "event_types_json": ["daily_summary_generated"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert data["name"] == "daily-hook"
    assert data["url"] == "https://public.example/hook"
    assert data["event_subscriptions_json"] == [
        {"event": "daily_summary_generated", "version": "1.0"},
    ]
    assert "event_types_json" not in data

    persisted = pg_db.query(WebhookConfig).filter(WebhookConfig.name == "daily-hook").one()
    assert persisted.event_subscriptions_json == [
        {"event": "daily_summary_generated", "version": "1.0"},
    ]
    # Legacy field must not survive on the persisted row.
    assert getattr(persisted, "event_types_json", None) is None


def test_create_webhook_rejects_loopback_url(client: TestClient, pg_db: Session) -> None:
    response = client.post(
        "/api/v1/webhooks",
        json={"name": "loopback", "url": "http://127.0.0.1/hook"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == 4000
    assert payload["data"] is None
    assert pg_db.query(WebhookConfig).count() == 0


def test_create_webhook_rejects_private_network_url(client: TestClient, pg_db: Session) -> None:
    response = client.post(
        "/api/v1/webhooks",
        json={"name": "private", "url": "http://10.0.0.1/hook"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == 4000
    assert payload["data"] is None
    assert pg_db.query(WebhookConfig).count() == 0


def test_create_webhook_returns_401_when_unauthenticated(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.post(
        "/api/v1/webhooks",
        json={"name": "no-auth", "url": "https://public.example/hook"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# PUT /api/v1/webhooks/{id} — update
# ---------------------------------------------------------------------------


def test_update_webhook_applies_changes(
    client: TestClient, pg_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_public_dns(monkeypatch)
    hook = _seed_webhook(
        pg_db,
        name="old-name",
        url="https://old.example/hook",
        enabled=False,
        event_subscriptions_json=[{"event": "all", "version": ""}],
    )

    response = client.put(
        f"/api/v1/webhooks/{hook.id}",
        json={
            "name": "new-name",
            "enabled": True,
            "event_subscriptions_json": [
                {"event": "daily_summary_generated", "version": "1.0"},
                {"event": "session_analyzed", "version": "1.0"},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert data["id"] == hook.id
    assert data["name"] == "new-name"
    assert data["enabled"] is True
    assert data["event_subscriptions_json"] == [
        {"event": "daily_summary_generated", "version": "1.0"},
        {"event": "session_analyzed", "version": "1.0"},
    ]
    assert data["url"] == "https://old.example/hook"


def test_update_webhook_returns_404_when_not_found(
    client: TestClient,
) -> None:
    response = client.put(
        "/api/v1/webhooks/9999",
        json={"name": "missing"},
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == 4002
    assert payload["data"] is None


def test_update_webhook_returns_401_when_unauthenticated(
    anonymous_client: TestClient, pg_db: Session
) -> None:
    hook = _seed_webhook(pg_db)

    response = anonymous_client.put(
        f"/api/v1/webhooks/{hook.id}",
        json={"name": "noop"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/v1/webhooks/{id} — delete
# ---------------------------------------------------------------------------


def test_delete_webhook_removes_row(client: TestClient, pg_db: Session) -> None:
    hook = _seed_webhook(pg_db, name="to-delete")

    response = client.delete(f"/api/v1/webhooks/{hook.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"] == {}
    assert pg_db.query(WebhookConfig).filter(WebhookConfig.id == hook.id).one_or_none() is None


def test_delete_webhook_is_idempotent_for_missing_id(client: TestClient) -> None:
    response = client.delete("/api/v1/webhooks/9999")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"] == {}


def test_delete_webhook_returns_401_when_unauthenticated(
    anonymous_client: TestClient, pg_db: Session
) -> None:
    hook = _seed_webhook(pg_db)

    response = anonymous_client.delete(f"/api/v1/webhooks/{hook.id}")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/{id}/test — dispatch test event
# ---------------------------------------------------------------------------


def test_test_webhook_dispatches_test_event_command(
    client: TestClient,
    pg_db: Session,
    fake_dispatcher: FakeTaskDispatcher,
) -> None:
    hook = _seed_webhook(pg_db, name="test-hook")

    response = client.post(f"/api/v1/webhooks/{hook.id}/test")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"] == {
        "success": True,
        "message": "Test webhook scheduled.",
    }

    assert len(fake_dispatcher.dispatched_webhook) == 1
    command = fake_dispatcher.dispatched_webhook[0]
    assert isinstance(command, SendWebhookCommand)
    assert command.event_type == "test_event"
    assert command.webhook_id == hook.id
    assert isinstance(command.payload, dict)
    assert command.payload["event"] == "test_event"


def test_test_webhook_returns_404_when_not_found(
    client: TestClient,
    fake_dispatcher: FakeTaskDispatcher,
) -> None:
    response = client.post("/api/v1/webhooks/9999/test")

    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == 4002
    assert payload["data"] is None
    assert fake_dispatcher.dispatched_webhook == []


def test_test_webhook_returns_401_when_unauthenticated(
    anonymous_client: TestClient, pg_db: Session
) -> None:
    hook = _seed_webhook(pg_db)

    response = anonymous_client.post(f"/api/v1/webhooks/{hook.id}/test")

    assert response.status_code == 401
