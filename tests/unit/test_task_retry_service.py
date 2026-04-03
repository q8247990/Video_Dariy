from datetime import datetime
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.services.task_retry import RetryResult, retry_task


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    VideoSession.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def _mock_orchestrator() -> MagicMock:
    orch = MagicMock()
    orch.dispatch_session_build.return_value = "build-task-1"
    orch.dispatch_analyze_session.return_value = "analysis-task-1"
    orch.dispatch_generate_daily_summary.return_value = "summary-task-1"
    return orch


def test_retry_rejects_non_retryable_status() -> None:
    db = _new_db_session()
    try:
        log = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            status=TaskStatus.RUNNING,
        )
        db.add(log)
        db.commit()

        result = retry_task(db, log, _mock_orchestrator())

        assert isinstance(result, RetryResult)
        assert result.success is False
        assert result.error_code == 4004
        assert "failed/timeout/cancelled" in result.error_message
    finally:
        db.close()


def test_retry_detects_duplicate_active_task() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="cam1",
            camera_name="cam1",
            location_name="door",
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
            detail_json={"scan_mode": "hot", "dedupe_key": dedupe_key},
        )
        running = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=source.id,
            status=TaskStatus.RUNNING,
            queue_task_id="existing-task",
            detail_json={"scan_mode": "hot", "dedupe_key": dedupe_key},
        )
        db.add_all([failed, running])
        db.commit()

        result = retry_task(db, failed, _mock_orchestrator())

        assert result.success is False
        assert result.error_code == 4004
        assert "already running" in result.error_message
    finally:
        db.close()


def test_retry_session_build() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="cam1",
            camera_name="cam1",
            location_name="door",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
            last_validate_status="success",
        )
        db.add(source)
        db.flush()

        log = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=source.id,
            status=TaskStatus.FAILED,
            detail_json={"scan_mode": "hot"},
        )
        db.add(log)
        db.commit()

        orch = _mock_orchestrator()
        result = retry_task(db, log, orch)

        assert result.success is True
        assert result.task_id == "build-task-1"
        orch.dispatch_session_build.assert_called_once()
    finally:
        db.close()


def test_retry_session_analysis_resets_to_sealed() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="cam1",
            camera_name="cam1",
            location_name="door",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
            last_validate_status="success",
        )
        db.add(source)
        db.flush()

        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 16, 12, 0, 0),
            session_end_time=datetime(2026, 3, 16, 12, 5, 0),
            total_duration_seconds=300,
            analysis_status=SessionAnalysisStatus.FAILED,
            analysis_priority="full",
        )
        db.add(session)
        db.flush()

        log = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session.id,
            status=TaskStatus.FAILED,
            detail_json={"priority": "full"},
        )
        db.add(log)
        db.commit()

        orch = _mock_orchestrator()
        result = retry_task(db, log, orch)

        assert result.success is True
        assert result.task_id == "analysis-task-1"
        db.refresh(session)
        assert session.analysis_status == SessionAnalysisStatus.SEALED
        orch.dispatch_analyze_session.assert_called_once()
    finally:
        db.close()


def test_retry_daily_summary() -> None:
    db = _new_db_session()
    try:
        log = TaskLog(
            task_type=TaskType.DAILY_SUMMARY_GENERATION,
            task_target_id=None,
            status=TaskStatus.FAILED,
            detail_json={"target_date": "2026-03-10"},
        )
        db.add(log)
        db.commit()

        orch = _mock_orchestrator()
        result = retry_task(db, log, orch)

        assert result.success is True
        assert result.task_id == "summary-task-1"
        orch.dispatch_generate_daily_summary.assert_called_once()
    finally:
        db.close()


def test_retry_unsupported_task_type() -> None:
    db = _new_db_session()
    try:
        log = TaskLog(
            task_type=TaskType.WEBHOOK_PUSH,
            task_target_id=None,
            status=TaskStatus.FAILED,
        )
        db.add(log)
        db.commit()

        result = retry_task(db, log, _mock_orchestrator())

        assert result.success is False
        assert result.error_code == 4004
        assert "does not support retry" in result.error_message
    finally:
        db.close()
