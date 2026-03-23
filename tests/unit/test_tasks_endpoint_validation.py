from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.v1.endpoints.tasks import trigger_analyze, trigger_full_build
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    VideoSession.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def _current_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


def test_trigger_full_build_rejects_disabled_source() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="客厅",
            camera_name="cam1",
            location_name="客厅",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=False,
            last_validate_status="success",
        )
        db.add(source)
        db.commit()

        resp = trigger_full_build(db=db, current_user=_current_user(), id=source.id)
        assert resp.code == 4004
        assert "disabled" in str(resp.message).lower()
    finally:
        db.close()


def test_trigger_analyze_rejects_open_session() -> None:
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

        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 16, 10, 0, 0),
            session_end_time=datetime(2026, 3, 16, 10, 5, 0),
            total_duration_seconds=300,
            analysis_status=SessionAnalysisStatus.OPEN,
        )
        db.add(session)
        db.commit()

        resp = trigger_analyze(db=db, current_user=_current_user(), session_id=session.id)
        assert resp.code == 4004
        assert "cannot be analyzed" in str(resp.message)
    finally:
        db.close()


def test_trigger_analyze_allows_paused_source_session(monkeypatch) -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="书房",
            camera_name="cam4",
            location_name="书房",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
            source_paused=True,
            last_validate_status="success",
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 16, 10, 0, 0),
            session_end_time=datetime(2026, 3, 16, 10, 5, 0),
            total_duration_seconds=300,
            analysis_status=SessionAnalysisStatus.SUCCESS,
        )
        db.add(session)
        db.commit()

        monkeypatch.setattr(
            "src.api.v1.endpoints.tasks._pipeline_orchestrator.dispatch_analyze_session",
            lambda command: "task-123",
        )

        resp = trigger_analyze(db=db, current_user=_current_user(), session_id=session.id)
        assert resp.code == 0
        assert resp.data["task_id"] == "task-123"
    finally:
        db.close()
