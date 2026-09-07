"""HTTP-level contract tests for ``src/api/v1/endpoints/tasks.py``.

This module replaces the previous
``tests/unit/test_tasks_endpoint_validation.py`` +
``tests/unit/test_task_control_endpoints.py`` direct-endpoint-call
approach. The new contract:

* Hit the real FastAPI app through :class:`fastapi.testclient.TestClient`
  — no monkeypatching endpoint bodies.
* Mount only the tasks router via :func:`tests.integration.conftest.make_http_client`.
* Inject a hermetic :class:`~src.application.bootstrap.Container` via
  :func:`src.api.deps.set_container_for_tests` so the
  :func:`src.api.deps.get_pipeline_orchestrator` dependency picks up the
  :class:`~src.application.bootstrap_fakes.FakeTaskDispatcher` / fake task
  control — orchestrator / use case code paths are exercised end to end.
* Use :class:`~src.api.error_status.ResponseStatusMiddleware` so that the
  business-code → HTTP-status mapping (4002 → 404, 4004 → 409, 5001 → 502)
  is asserted at the HTTP layer, not just the JSON body.

Endpoints covered:

* ``POST /api/v1/tasks/{id}/build/full``
* ``POST /api/v1/tasks/analyze/{session_id}``
* ``POST /api/v1/tasks/logs/{id}/stop``
* ``POST /api/v1/tasks/logs/{id}/retry``
* ``DELETE /api/v1/tasks/logs/{id}``

Assertions focus on the **port contract** (HTTP status + JSON body +
dispatcher recorded command shape) rather than the fake's private state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import src.api.deps as api_deps
from src.api.error_status import ResponseStatusMiddleware
from src.api.v1.endpoints import tasks
from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeTaskDispatcher
from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    SessionBuildCommand,
)
from src.application.ports.task_control import TaskControlPort
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    ScanMode,
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)

# ---------------------------------------------------------------------------
# Container teardown helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_dispatcher() -> Iterator[FakeTaskDispatcher]:
    """Swap the API layer's composition-root container with a fake-bound one.

    The tasks router builds its orchestrator on every request from
    ``api_deps.get_pipeline_orchestrator(container)`` which reads
    ``api_deps._container.dispatcher`` each call — replacing the module-level
    container is enough to route every request through the
    :class:`FakeTaskDispatcher`.

    Teardown restores the production container captured at setup time, so
    later tests in the session see a clean module-level singleton. We
    deliberately do not rebuild via ``bootstrap_production`` here because
    other tests in the same pytest session may have already replaced the
    container and we want to be a good neighbour.
    """

    fake = FakeTaskDispatcher()
    original = api_deps._container
    api_deps.set_container_for_tests(bootstrap_for_tests(dispatcher=fake))
    try:
        yield fake
    finally:
        api_deps._container = original


@pytest.fixture
def client(
    pg_db: Session, make_http_client, fake_dispatcher: FakeTaskDispatcher
) -> Iterator[TestClient]:
    """Authenticated client for the tasks router with the fake dispatcher wired."""

    del fake_dispatcher  # rely on fixture side-effect to swap the container
    with make_http_client(
        [(tasks.router, "/api/v1/tasks")],
        middleware=[ResponseStatusMiddleware],
    ) as test_client:
        yield test_client


@pytest.fixture
def anonymous_client(pg_db: Session, make_http_client) -> Iterator[TestClient]:
    """Unauthenticated client: no admin user override → 401 path."""

    with make_http_client(
        [(tasks.router, "/api/v1/tasks")],
        authenticated=False,
        middleware=[ResponseStatusMiddleware],
    ) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    pg_db: Session,
    *,
    name: str,
    enabled: bool = True,
    paused: bool = False,
) -> VideoSource:
    source = VideoSource(
        source_name=name,
        camera_name=f"{name}-cam",
        location_name=name,
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=enabled,
        source_paused=paused,
        last_validate_status="success",
    )
    pg_db.add(source)
    pg_db.commit()
    pg_db.refresh(source)
    return source


def _make_session(
    pg_db: Session,
    source_id: int,
    *,
    status: SessionAnalysisStatus,
    priority: str = "hot",
) -> VideoSession:
    session = VideoSession(
        source_id=source_id,
        session_start_time=datetime(2026, 3, 16, 10, 0, 0),
        session_end_time=datetime(2026, 3, 16, 10, 5, 0),
        total_duration_seconds=300,
        analysis_status=status,
        analysis_priority=priority,
    )
    pg_db.add(session)
    pg_db.commit()
    pg_db.refresh(session)
    return session


def _make_task_log(
    pg_db: Session,
    *,
    task_type: TaskType,
    target_id: int | None,
    status: TaskStatus,
    queue_task_id: str | None = None,
    detail_json: dict | None = None,
) -> TaskLog:
    log = TaskLog(
        task_type=task_type,
        task_target_id=target_id,
        status=status,
        queue_task_id=queue_task_id,
        detail_json=detail_json,
    )
    pg_db.add(log)
    pg_db.commit()
    pg_db.refresh(log)
    return log


# ===========================================================================
# POST /api/v1/tasks/{id}/build/full
# ===========================================================================


def test_build_full_unauthenticated_returns_401(anonymous_client: TestClient) -> None:
    response = anonymous_client.post("/api/v1/tasks/1/build/full")

    assert response.status_code == 401


def test_build_full_with_invalid_path_segment_returns_422(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/tasks/not-an-int/build/full")

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


def test_build_full_returns_404_when_source_missing(client: TestClient, pg_db: Session) -> None:
    del pg_db
    response = client.post("/api/v1/tasks/999999/build/full")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 4002


def test_build_full_rejects_disabled_source_with_409(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    source = _make_source(pg_db, name="客厅", enabled=False)

    response = client.post(f"/api/v1/tasks/{source.id}/build/full")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004
    assert "disabled" in body["message"].lower()
    assert fake_dispatcher.dispatched_session_build == []


def test_build_full_rejects_when_full_scan_already_running(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    source = _make_source(pg_db, name="门口")
    _make_task_log(
        pg_db,
        task_type=TaskType.SESSION_BUILD,
        target_id=source.id,
        status=TaskStatus.RUNNING,
        queue_task_id="active-full-build",
        detail_json={"scan_mode": ScanMode.FULL.value},
    )

    response = client.post(f"/api/v1/tasks/{source.id}/build/full")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004
    assert fake_dispatcher.dispatched_session_build == []


def test_build_full_dispatches_full_scan_session_build_command(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    source = _make_source(pg_db, name="书房")

    response = client.post(f"/api/v1/tasks/{source.id}/build/full")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["task_id"] == "fake-task-1"
    assert fake_dispatcher.dispatched_session_build == [
        SessionBuildCommand(source_id=source.id, scan_mode=ScanMode.FULL.value)
    ]


# ===========================================================================
# POST /api/v1/tasks/analyze/{session_id}
# ===========================================================================


def test_analyze_unauthenticated_returns_401(anonymous_client: TestClient) -> None:
    response = anonymous_client.post("/api/v1/tasks/analyze/1")

    assert response.status_code == 401


def test_analyze_with_invalid_path_segment_returns_422(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/tasks/analyze/abc")

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


def test_analyze_returns_404_when_session_missing(client: TestClient, pg_db: Session) -> None:
    del pg_db
    response = client.post("/api/v1/tasks/analyze/424242")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 4002


def test_analyze_rejects_open_session_with_409(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    source = _make_source(pg_db, name="门口")
    session = _make_session(pg_db, source.id, status=SessionAnalysisStatus.OPEN)

    response = client.post(f"/api/v1/tasks/analyze/{session.id}")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004
    assert "cannot be analyzed" in body["message"]
    assert fake_dispatcher.dispatched_analyze_session == []


def test_analyze_rejects_analyzing_session_with_409(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    source = _make_source(pg_db, name="客厅")
    session = _make_session(pg_db, source.id, status=SessionAnalysisStatus.ANALYZING)

    response = client.post(f"/api/v1/tasks/analyze/{session.id}")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004
    assert fake_dispatcher.dispatched_analyze_session == []


def test_analyze_allows_paused_source_and_resets_success_session_to_sealed(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    source = _make_source(pg_db, name="书房", paused=True)
    session = _make_session(
        pg_db,
        source.id,
        status=SessionAnalysisStatus.SUCCESS,
        priority="full",
    )

    response = client.post(f"/api/v1/tasks/analyze/{session.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["task_id"] == "fake-task-1"
    assert fake_dispatcher.dispatched_analyze_session == [
        AnalyzeSessionCommand(session_id=session.id, priority="full")
    ]
    pg_db.refresh(session)
    assert session.analysis_status == SessionAnalysisStatus.SEALED


# ===========================================================================
# POST /api/v1/tasks/logs/{id}/stop
# ===========================================================================


def test_stop_unauthenticated_returns_401(anonymous_client: TestClient) -> None:
    response = anonymous_client.post("/api/v1/tasks/logs/1/stop")

    assert response.status_code == 401


def test_stop_with_invalid_path_segment_returns_422(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/tasks/logs/not-int/stop")

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


def test_stop_returns_404_when_task_log_missing(client: TestClient, pg_db: Session) -> None:
    del pg_db
    response = client.post("/api/v1/tasks/logs/987654/stop")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 4002


def test_stop_running_task_returns_200_with_cancel_requested_payload(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    del fake_dispatcher  # stop uses task_control, not dispatcher
    source = _make_source(pg_db, name="客厅")
    session = _make_session(pg_db, source.id, status=SessionAnalysisStatus.ANALYZING)
    log = _make_task_log(
        pg_db,
        task_type=TaskType.SESSION_ANALYSIS,
        target_id=session.id,
        status=TaskStatus.RUNNING,
        queue_task_id="running-analysis-task",
    )

    response = client.post(f"/api/v1/tasks/logs/{log.id}/stop")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["task_log_id"] == log.id
    assert body["data"]["cancel_requested"] is True
    assert body["data"]["status"] == TaskStatus.RUNNING.value


def test_stop_non_active_task_returns_409(client: TestClient, pg_db: Session) -> None:
    log = _make_task_log(
        pg_db,
        task_type=TaskType.SESSION_BUILD,
        target_id=1,
        status=TaskStatus.SUCCESS,
    )

    response = client.post(f"/api/v1/tasks/logs/{log.id}/stop")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004


# ===========================================================================
# POST /api/v1/tasks/logs/{id}/retry
# ===========================================================================


def test_retry_unauthenticated_returns_401(anonymous_client: TestClient) -> None:
    response = anonymous_client.post("/api/v1/tasks/logs/1/retry")

    assert response.status_code == 401


def test_retry_with_invalid_path_segment_returns_422(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/tasks/logs/abc/retry")

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


def test_retry_returns_404_when_task_log_missing(client: TestClient, pg_db: Session) -> None:
    del pg_db
    response = client.post("/api/v1/tasks/logs/424242/retry")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 4002


def test_retry_session_analysis_dispatches_analyze_command_and_resets_session(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    source = _make_source(pg_db, name="客厅")
    session = _make_session(
        pg_db,
        source.id,
        status=SessionAnalysisStatus.FAILED,
        priority="full",
    )
    log = _make_task_log(
        pg_db,
        task_type=TaskType.SESSION_ANALYSIS,
        target_id=session.id,
        status=TaskStatus.FAILED,
        detail_json={"priority": "full"},
    )

    response = client.post(f"/api/v1/tasks/logs/{log.id}/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["task_id"] == "fake-task-1"
    assert fake_dispatcher.dispatched_analyze_session == [
        AnalyzeSessionCommand(session_id=session.id, priority="full")
    ]
    pg_db.refresh(session)
    assert session.analysis_status == SessionAnalysisStatus.SEALED


def test_retry_rejects_duplicate_running_with_409_and_existing_task_id(
    client: TestClient, pg_db: Session, fake_dispatcher: FakeTaskDispatcher
) -> None:
    source = _make_source(pg_db, name="门口")
    dedupe_key = f"session_build|{source.id}|hot"
    failed = _make_task_log(
        pg_db,
        task_type=TaskType.SESSION_BUILD,
        target_id=source.id,
        status=TaskStatus.FAILED,
        detail_json={"scan_mode": "hot", "dedupe_key": dedupe_key},
    )
    _make_task_log(
        pg_db,
        task_type=TaskType.SESSION_BUILD,
        target_id=source.id,
        status=TaskStatus.RUNNING,
        queue_task_id="existing-build-task",
        detail_json={"scan_mode": "hot", "dedupe_key": dedupe_key},
    )

    response = client.post(f"/api/v1/tasks/logs/{failed.id}/retry")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004
    assert "already running" in body["message"]
    assert body["data"]["task_id"] == "existing-build-task"
    assert fake_dispatcher.dispatched_session_build == []


# ===========================================================================
# DELETE /api/v1/tasks/logs/{id}
# ===========================================================================


def test_delete_unauthenticated_returns_401(anonymous_client: TestClient) -> None:
    response = anonymous_client.delete("/api/v1/tasks/logs/1")

    assert response.status_code == 401


def test_delete_with_invalid_path_segment_returns_422(
    client: TestClient,
) -> None:
    response = client.delete("/api/v1/tasks/logs/not-int")

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


def test_delete_returns_404_when_task_log_missing(client: TestClient, pg_db: Session) -> None:
    del pg_db
    response = client.delete("/api/v1/tasks/logs/424242")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 4002


def test_delete_running_task_returns_409(client: TestClient, pg_db: Session) -> None:
    log = _make_task_log(
        pg_db,
        task_type=TaskType.SESSION_BUILD,
        target_id=1,
        status=TaskStatus.RUNNING,
    )

    response = client.delete(f"/api/v1/tasks/logs/{log.id}")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004
    assert pg_db.query(TaskLog).filter(TaskLog.id == log.id).one() is not None


def test_delete_finished_task_returns_200_and_removes_row(
    client: TestClient, pg_db: Session
) -> None:
    log = _make_task_log(
        pg_db,
        task_type=TaskType.SESSION_BUILD,
        target_id=1,
        status=TaskStatus.SUCCESS,
    )

    response = client.delete(f"/api/v1/tasks/logs/{log.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert pg_db.query(TaskLog).filter(TaskLog.id == log.id).one_or_none() is None


# ===========================================================================
# 5001 surface: revoke failure mapped to HTTP 502
#
# The stop endpoint maps a broker error from the TaskControlPort to a
# 5001 business code which ResponseStatusMiddleware turns into HTTP 502.
# We override the container with a fake task control that raises on
# revoke, exercising the same port path the production adapter uses.
# ===========================================================================


def test_stop_returns_502_when_revoke_raises(
    pg_db: Session,
    make_http_client,
) -> None:
    """Stop hits the broker through TaskControlPort; a broker failure → 502."""

    class _ExplodingTaskControl(TaskControlPort):
        def revoke(self, task_id: str, *, terminate: bool = False) -> None:
            raise RuntimeError("broker unreachable")

        def heartbeat(self, queue: str = ""):  # pragma: no cover - unused
            raise NotImplementedError

    exploding = _ExplodingTaskControl()
    original = api_deps._container
    api_deps.set_container_for_tests(
        bootstrap_for_tests(
            dispatcher=FakeTaskDispatcher(),
            task_control=exploding,
        )
    )
    try:
        source = _make_source(pg_db, name="客厅")
        session = _make_session(pg_db, source.id, status=SessionAnalysisStatus.ANALYZING)
        log = _make_task_log(
            pg_db,
            task_type=TaskType.SESSION_ANALYSIS,
            target_id=session.id,
            status=TaskStatus.RUNNING,
            queue_task_id="will-fail-to-revoke",
        )
        with make_http_client(
            [(tasks.router, "/api/v1/tasks")],
            middleware=[ResponseStatusMiddleware],
        ) as test_client:
            response = test_client.post(f"/api/v1/tasks/logs/{log.id}/stop")

        assert response.status_code == 502
        body = response.json()
        assert body["code"] == 5001
        assert "broker unreachable" in body["message"]
    finally:
        api_deps._container = original
