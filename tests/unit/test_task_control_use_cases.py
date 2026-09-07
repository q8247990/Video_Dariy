"""Use-case unit tests for the stop / retry task-log endpoints.

These tests cover the ``stop_task_log_use_case`` and
``retry_task_log_use_case`` orchestration directly, complementing the
HTTP-level contract tests under ``tests/integration/test_tasks_http.py``.
The endpoint behaviour (status codes, JSON shape, auth) is asserted at
the HTTP layer; here we focus on the use-case outcomes the endpoint
relies on:

* ``stop_task_log_use_case`` flips ``cancel_requested``, leaves the row
  status alone for ``RUNNING`` / flips to ``CANCELLED`` for ``PENDING``,
  records a user-facing message, and never mutates the target session.
* ``retry_task_log_use_case`` rejects duplicates with the conflicting
  task's id, and resets a failed analysis session back to ``SEALED``
  before dispatching a fresh analyze command.
* ``stop_task_log_use_case`` surfaces broker errors as ``5001`` and does
  **not** mutate the row when the revoke call raises.

All paths use the port-level fakes shipped in
``src/application/bootstrap_fakes`` — no Celery, no Redis, no HTTP.
The composition root comes from ``bootstrap_for_tests()`` so the use
cases bind to the same in-memory ports the integration tests do.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeTaskControl, FakeTaskDispatcher
from src.application.tasks import retry_task_log_use_case, stop_task_log_use_case
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)

# ---------------------------------------------------------------------------
# stop_task_log_use_case
# ---------------------------------------------------------------------------


def test_stop_running_analysis_marks_cancel_requested_without_resetting_session(
    pg_db: Session,
) -> None:
    source = VideoSource(
        source_name="客厅",
        camera_name="cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
        last_validate_status="success",
    )
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 10, 8, 0, 0),
        session_end_time=datetime(2026, 3, 10, 8, 5, 0),
        total_duration_seconds=300,
        analysis_status=SessionAnalysisStatus.ANALYZING,
    )
    pg_db.add(session)
    pg_db.flush()
    log = TaskLog(
        task_type=TaskType.SESSION_ANALYSIS,
        task_target_id=session.id,
        queue_task_id="task-1",
        status=TaskStatus.RUNNING,
    )
    pg_db.add(log)
    pg_db.commit()

    container = bootstrap_for_tests()

    resp = stop_task_log_use_case(db=pg_db, task_log_id=log.id, locale="zh-CN", container=container)

    assert resp.error_code == 0
    assert resp.payload is not None
    assert resp.payload["task_log_id"] == log.id
    assert resp.payload["status"] == TaskStatus.RUNNING.value
    assert resp.payload["cancel_requested"] is True

    pg_db.commit()
    pg_db.refresh(log)
    pg_db.refresh(session)
    assert log.status == TaskStatus.RUNNING
    assert log.cancel_requested is True
    assert log.message == "Cancellation requested by user"
    assert session.analysis_status == SessionAnalysisStatus.ANALYZING


def test_stop_task_log_returns_4004_when_not_active(pg_db: Session) -> None:
    log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.SUCCESS,
    )
    pg_db.add(log)
    pg_db.commit()

    container = bootstrap_for_tests()

    resp = stop_task_log_use_case(db=pg_db, task_log_id=log.id, locale="zh-CN", container=container)

    assert resp.error_code == 4004
    assert resp.payload is None


def test_stop_task_log_returns_5001_on_revoke_error(pg_db: Session) -> None:
    """When the broker revoke raises, the use case surfaces 5001 without
    touching the row."""

    log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        queue_task_id="bad-task",
        status=TaskStatus.RUNNING,
    )
    pg_db.add(log)
    pg_db.commit()

    class _ExplodingTaskControl:
        def revoke(self, task_id: str, *, terminate: bool = False) -> None:
            raise RuntimeError("broker unreachable")

        def heartbeat(self, queue: str = ""):  # pragma: no cover - unused
            raise NotImplementedError

    container = bootstrap_for_tests(task_control=_ExplodingTaskControl())  # type: ignore[arg-type]

    resp = stop_task_log_use_case(db=pg_db, task_log_id=log.id, locale="zh-CN", container=container)

    assert resp.error_code == 5001
    assert "broker unreachable" in resp.error_message
    assert resp.payload is None

    pg_db.refresh(log)
    assert log.cancel_requested is False
    assert log.status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# retry_task_log_use_case
# ---------------------------------------------------------------------------


def test_retry_task_log_rejects_duplicate_running(pg_db: Session) -> None:
    source = VideoSource(
        source_name="门口",
        camera_name="cam2",
        location_name="门口",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
        last_validate_status="success",
    )
    pg_db.add(source)
    pg_db.flush()
    dedupe_key = f"session_build|{source.id}|hot"

    failed = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source.id,
        status=TaskStatus.FAILED,
        detail_json={"scan_mode": "hot", "dedupe_key": dedupe_key},
    )
    running = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source.id,
        status=TaskStatus.RUNNING,
        queue_task_id="existing-task",
        detail_json={"scan_mode": "hot", "dedupe_key": dedupe_key},
    )
    pg_db.add_all([failed, running])
    pg_db.commit()

    container = bootstrap_for_tests()

    resp = retry_task_log_use_case(
        db=pg_db, task_log_id=failed.id, locale="zh-CN", container=container
    )

    assert resp.error_code == 4004
    assert "already running" in resp.error_message
    assert resp.task_id == "existing-task"


def test_retry_analysis_task_resets_failed_session_to_sealed(pg_db: Session) -> None:
    source = VideoSource(
        source_name="客厅",
        camera_name="cam3",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
        last_validate_status="success",
    )
    pg_db.add(source)
    pg_db.flush()

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 16, 12, 0, 0),
        session_end_time=datetime(2026, 3, 16, 12, 5, 0),
        total_duration_seconds=300,
        analysis_status=SessionAnalysisStatus.FAILED,
        analysis_priority="full",
    )
    pg_db.add(session)
    pg_db.flush()

    failed = TaskLog(
        task_type=TaskType.SESSION_ANALYSIS,
        task_target_id=session.id,
        status=TaskStatus.FAILED,
        detail_json={"priority": "full"},
    )
    pg_db.add(failed)
    pg_db.commit()

    fake_dispatcher = FakeTaskDispatcher()
    fake_dispatcher.set_next_return("analysis-task-1")
    container = bootstrap_for_tests(dispatcher=fake_dispatcher)

    resp = retry_task_log_use_case(
        db=pg_db, task_log_id=failed.id, locale="zh-CN", container=container
    )

    assert resp.error_code == 0
    assert resp.task_id == "analysis-task-1"
    assert len(fake_dispatcher.dispatched_analyze_session) == 1
    command = fake_dispatcher.dispatched_analyze_session[0]
    assert command.session_id == session.id
    assert command.priority == "full"

    pg_db.commit()
    pg_db.refresh(session)
    assert session.analysis_status == SessionAnalysisStatus.SEALED


# ---------------------------------------------------------------------------
# Port-satisfaction guard
# ---------------------------------------------------------------------------


def test_bootstrap_defaults_satisfy_task_control_and_dispatcher_ports() -> None:
    """The fakes used by the tests above satisfy the runtime port Protocols
    so composition-root ``_verify_port`` does not reject them."""

    container = bootstrap_for_tests()

    assert isinstance(container.task_control, FakeTaskControl)
    assert isinstance(container.dispatcher, FakeTaskDispatcher)
