"""Tests for the /healthz endpoint (todo 15 scaffold, detector semantics todo 16)."""

from __future__ import annotations

from pathlib import Path

import pytest
from app import main
from app.config import get_settings
from fastapi.testclient import TestClient
from helpers import build_constant_onnx, make_yolo_output

_SAMPLER_ENV_VARS = (
    "SAMPLER_HOST",
    "SAMPLER_PORT",
    "SAMPLER_ALLOWED_ROOTS",
    "SAMPLER_MODEL_PATH",
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
    assert set(response.json().keys()) == {"status", "model_loaded", "version"}


def test_healthz_reports_unloaded_model_and_default_version() -> None:
    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.json() == {"status": "ok", "model_loaded": False, "version": "0.1.0"}


def test_healthz_model_loaded_true_with_valid_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_file = build_constant_onnx(tmp_path / "yolov8n.onnx", make_yolo_output())
    monkeypatch.setenv("SAMPLER_MODEL_PATH", str(model_file))

    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.json()["model_loaded"] is True


def test_healthz_invalid_model_file_reports_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_file = tmp_path / "junk.onnx"
    model_file.write_bytes(b"not-a-real-model")
    monkeypatch.setenv("SAMPLER_MODEL_PATH", str(model_file))

    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["model_loaded"] is False


def test_healthz_missing_model_reports_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMPLER_MODEL_PATH", "/nonexistent/model.onnx")

    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["model_loaded"] is False


def test_healthz_reports_version_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMPLER_VERSION", "9.9.9")

    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.json()["version"] == "9.9.9"
