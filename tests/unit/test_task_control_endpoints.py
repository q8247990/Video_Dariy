from datetime import datetime

from sqlalchemy.orm import Session

from src.application.bootstrap import bootstrap_for_tests
from src.application.tasks import retry_task_log_use_case, stop_task_log_use_case
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType


def test_stop_analysis_task_marks_cancel_requested_without_resetting_session(
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

    resp = stop_task_log_use_case(
        db=pg_db, task_log_id=log.id, locale="zh-CN", container=container
    )

    assert resp.error_code == 0
    assert resp.payload == {
        "task_log_id": log.id,
        "status": TaskStatus.RUNNING,
        "cancel_requested": True,
    }
    pg_db.commit()
    pg_db.refresh(log)
    pg_db.refresh(session)
    assert log.status == TaskStatus.RUNNING
    assert log.cancel_requested is True
    assert log.message == "Cancellation requested by user"
    assert session.analysis_status == SessionAnalysisStatus.ANALYZING
    # Fake task control recorded the revoke call (terminate=True).
    from src.application.bootstrap_fakes import FakeTaskControl

    fake_task_control = container.task_control
    assert isinstance(fake_task_control, FakeTaskControl)
    assert fake_task_control.revocations == [("task-1", True)]


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
        detail_json={
            "scan_mode": "hot",
            "dedupe_key": dedupe_key,
        },
    )
    running = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source.id,
        status=TaskStatus.RUNNING,
        queue_task_id="existing-task",
        detail_json={
            "scan_mode": "hot",
            "dedupe_key": dedupe_key,
        },
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

    from src.application.bootstrap_fakes import FakeTaskDispatcher

    fake_dispatcher = FakeTaskDispatcher()
    fake_dispatcher.set_next_return("analysis-task-1")
    container = bootstrap_for_tests(dispatcher=fake_dispatcher)

    resp = retry_task_log_use_case(
        db=pg_db, task_log_id=failed.id, locale="zh-CN", container=container
    )

    assert resp.error_code == 0
    assert resp.task_id == "analysis-task-1"
    assert fake_dispatcher.dispatched_analyze_session, "fake dispatcher received a dispatch"
    pg_db.commit()
    pg_db.refresh(session)
    assert session.analysis_status == SessionAnalysisStatus.SEALED


def test_stop_task_log_returns_4004_when_not_active(pg_db: Session) -> None:
    log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.SUCCESS,
    )
    pg_db.add(log)
    pg_db.commit()

    container = bootstrap_for_tests()

    resp = stop_task_log_use_case(
        db=pg_db, task_log_id=log.id, locale="zh-CN", container=container
    )

    assert resp.error_code == 4004
    assert resp.payload is None


def test_stop_task_log_returns_5001_on_revoke_error(pg_db: Session) -> None:
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

        def heartbeat(self, queue: str = ""):  # pragma: no cover - unused in this test
            raise NotImplementedError

    from src.application.ports.task_control import TaskControlPort

    if not isinstance(_ExplodingTaskControl(), TaskControlPort):
        raise AssertionError("exploding control must satisfy TaskControlPort Protocol")
    container = bootstrap_for_tests(task_control=_ExplodingTaskControl())

    resp = stop_task_log_use_case(
        db=pg_db, task_log_id=log.id, locale="zh-CN", container=container
    )

    assert resp.error_code == 5001
    assert "broker unreachable" in resp.error_message
    assert resp.payload is None


def test_delete_task_log_blocks_running_and_allows_finished(pg_db: Session) -> None:
    """Legacy delete-task endpoint smoke test, kept alongside the new
    use-case coverage. This endpoint never touched Celery directly, so
    it does not need a container — but we keep the call shape stable
    for consistency with the new endpoint signatures."""

    from types import SimpleNamespace

    from src.api.v1.endpoints.tasks import delete_task_log

    running = TaskLog(
        task_type=TaskType.SESSION_BUILD, task_target_id=1, status=TaskStatus.RUNNING
    )
    finished = TaskLog(
        task_type=TaskType.SESSION_BUILD, task_target_id=1, status=TaskStatus.SUCCESS
    )
    pg_db.add_all([running, finished])
    pg_db.commit()

    blocked = delete_task_log(
        db=pg_db,
        current_user=SimpleNamespace(id=1, username="admin"),
        locale="zh-CN",
        id=running.id,
    )
    assert blocked.code == 4004

    ok = delete_task_log(
        db=pg_db,
        current_user=SimpleNamespace(id=1, username="admin"),
        locale="zh-CN",
        id=finished.id,
    )
    assert ok.code == 0
