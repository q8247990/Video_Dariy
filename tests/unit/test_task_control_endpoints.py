from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.v1.endpoints.tasks import delete_task_log, retry_task_log, stop_task_log
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    VideoSession.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def _current_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


def test_stop_analysis_task_marks_cancel_requested_without_resetting_session(monkeypatch) -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="客厅",
            camera_name="cam1",
            location_name="客厅",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
            last_validate_status="success",
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 10, 8, 0, 0),
            session_end_time=datetime(2026, 3, 10, 8, 5, 0),
            total_duration_seconds=300,
            analysis_status=SessionAnalysisStatus.ANALYZING,
        )
        db.add(session)
        db.flush()
        log = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session.id,
            queue_task_id="task-1",
            status=TaskStatus.RUNNING,
        )
        db.add(log)
        db.commit()

        monkeypatch.setattr(
            "src.api.v1.endpoints.tasks.celery_app.control.revoke", lambda *args, **kwargs: None
        )

        resp = stop_task_log(db=db, current_user=_current_user(), id=log.id)
        assert resp.code == 0
        db.refresh(log)
        db.refresh(session)
        assert log.status == TaskStatus.RUNNING
        assert log.cancel_requested is True
        assert log.message == "Cancellation requested by user"
        assert session.analysis_status == SessionAnalysisStatus.ANALYZING
    finally:
        db.close()


def test_retry_task_log_rejects_duplicate_running(monkeypatch) -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="门口",
            camera_name="cam2",
            location_name="门口",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
            last_validate_status="success",
        )
        db.add(source)
        db.flush()
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
        db.add_all([failed, running])
        db.commit()

        monkeypatch.setattr(
            "src.api.v1.endpoints.tasks._pipeline_orchestrator.dispatch_session_build",
            lambda command: (_ for _ in ()).throw(RuntimeError("should not dispatch")),
        )

        resp = retry_task_log(db=db, current_user=_current_user(), id=failed.id)
        assert resp.code == 4004
        assert "already running" in str(resp.message)
    finally:
        db.close()


def test_delete_task_log_blocks_running_and_allows_finished() -> None:
    db = _new_db_session()
    try:
        running = TaskLog(
            task_type=TaskType.SESSION_BUILD, task_target_id=1, status=TaskStatus.RUNNING
        )
        finished = TaskLog(
            task_type=TaskType.SESSION_BUILD, task_target_id=1, status=TaskStatus.SUCCESS
        )
        db.add_all([running, finished])
        db.commit()

        blocked = delete_task_log(db=db, current_user=_current_user(), id=running.id)
        assert blocked.code == 4004

        ok = delete_task_log(db=db, current_user=_current_user(), id=finished.id)
        assert ok.code == 0
    finally:
        db.close()
