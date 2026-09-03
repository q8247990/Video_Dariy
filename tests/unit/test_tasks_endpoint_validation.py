from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from src.api.v1.endpoints.tasks import trigger_analyze, trigger_full_build
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus


def _current_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


def test_trigger_full_build_rejects_disabled_source(pg_db: Session) -> None:
    source = VideoSource(
        source_name="客厅",
        camera_name="cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=False,
        last_validate_status="success",
    )
    pg_db.add(source)
    pg_db.commit()

    mock_orchestrator = MagicMock()

    resp = trigger_full_build(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        id=source.id,
        orchestrator=mock_orchestrator,
    )
    assert resp.code == 4004
    assert "disabled" in str(resp.message).lower()


def test_trigger_analyze_rejects_open_session(pg_db: Session) -> None:
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

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 16, 10, 0, 0),
        session_end_time=datetime(2026, 3, 16, 10, 5, 0),
        total_duration_seconds=300,
        analysis_status=SessionAnalysisStatus.OPEN,
    )
    pg_db.add(session)
    pg_db.commit()

    mock_orchestrator = MagicMock()

    resp = trigger_analyze(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        session_id=session.id,
        orchestrator=mock_orchestrator,
    )
    assert resp.code == 4004
    assert "cannot be analyzed" in str(resp.message)


def test_trigger_analyze_allows_paused_source_session(pg_db: Session) -> None:
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
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 16, 10, 0, 0),
        session_end_time=datetime(2026, 3, 16, 10, 5, 0),
        total_duration_seconds=300,
        analysis_status=SessionAnalysisStatus.SUCCESS,
    )
    pg_db.add(session)
    pg_db.commit()

    mock_orchestrator = MagicMock()
    mock_orchestrator.dispatch_analyze_session.return_value = "task-123"

    resp = trigger_analyze(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        session_id=session.id,
        orchestrator=mock_orchestrator,
    )
    assert resp.code == 0
    assert resp.data["task_id"] == "task-123"
