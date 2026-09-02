"""Unit tests for the correlation middleware and the /metrics health surface.

The /metrics endpoint fans out to :func:`src.db.metrics.metrics_snapshot`;
here it is patched so no real database is required. The correlation
middleware asserts an ``X-Request-ID`` is echoed back and a fresh id is
minted when the caller sends none.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_metrics_returns_observability_gauges(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {
        "outbox": {
            "oldest_pending_seconds": 42,
            "pending_count": 3,
            "publishing_count": 0,
            "failed_count": 1,
        },
        "task_recovery": {
            "leases_recovered_total": 5,
            "last_heartbeat": {
                "leases_recovered": 2,
                "unleased_recovered": 4,
                "dispatched_hot": 0,
                "logs_deleted": 1,
                "missing_marked": 0,
            },
        },
        "analysis_checkpoint": [
            {"session_id": 7, "task_log_id": 99, "completed_sub_chunks": 2, "total_sub_chunks": 5}
        ],
    }
    monkeypatch.setattr("src.main.metrics_snapshot", lambda engine: snapshot)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.json() == snapshot


def test_metrics_reflects_outbox_lag_and_failed_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.metrics_snapshot",
        lambda engine: {"outbox": {"failed_count": 4, "oldest_pending_seconds": 300}},
    )
    body = client.get("/metrics").json()
    assert body["outbox"]["failed_count"] == 4
    assert body["outbox"]["oldest_pending_seconds"] == 300


def test_correlation_middleware_echoes_supplied_request_id() -> None:
    response = client.get("/livez", headers={"X-Request-ID": "corr-custom-123"})
    assert response.headers.get("X-Request-ID") == "corr-custom-123"


def test_correlation_middleware_mints_request_id_when_absent() -> None:
    response = client.get("/livez")
    request_id = response.headers.get("X-Request-ID")
    assert request_id
    assert len(request_id) == 36  # uuid4 hex
