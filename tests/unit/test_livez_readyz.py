"""Unit tests for liveness/readiness endpoints in ``src.main``.

``/livez`` is a pure process-liveness probe with no dependency. ``/readyz``
fans out to DB / Redis / Alembic-head checks; the check logic itself is
exercised indirectly here by patching ``src.main.readiness_checks`` (which is
the same callable wired into the endpoint) so no real Postgres / Redis is
required. The underlying check implementations are covered by the
PostgreSQL integration suite where a real engine is available.
"""

import pytest
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_livez_returns_ok_without_dependency() -> None:
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_returns_200_when_all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.main.readiness_checks",
        lambda: {"database": True, "redis": True, "alembic_head": True},
    )
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": True, "redis": True, "alembic_head": True}


def test_readyz_returns_503_when_database_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.main.readiness_checks",
        lambda: {"database": False, "redis": True, "alembic_head": False},
    )
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"database": False, "redis": True, "alembic_head": False}
