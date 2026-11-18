"""Tests for the /healthz endpoint (todo 15 scaffold)."""

from __future__ import annotations

import pytest
from app import main
from app.config import get_settings
from fastapi.testclient import TestClient

_SAMPLER_ENV_VARS = (
    "SAMPLER_HOST",
    "SAMPLER_PORT",
    "SAMPLER_ALLOWED_ROOTS",
    "SAMPLER_VERSION",
)


@pytest.fixture(autouse=True)
def clean_sampler_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a clean, deterministic environment."""
    for name in _SAMPLER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_healthz_returns_200_with_exact_keys() -> None:
    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert set(response.json().keys()) == {"status", "version"}


def test_healthz_reports_ok_and_default_version() -> None:
    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_healthz_reports_version_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMPLER_VERSION", "9.9.9")

    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.json()["version"] == "9.9.9"
