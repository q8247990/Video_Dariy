from datetime import datetime
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from src.application.tasks.use_case_retry import RetryResult, retry_task
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)


def _mock_dispatcher() -> MagicMock:
    dispatcher = MagicMock()
    dispatcher.dispatch_session_build.return_value = "build-task-1"
    dispatcher.dispatch_analyze_session.return_value = "analysis-task-1"
    dispatcher.dispatch_generate_daily_summary.return_value = "summary-task-1"
    return dispatcher


def test_retry_rejects_non_retryable_status(pg_db: Session) -> None:
    log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.RUNNING,
    )
    pg_db.add(log)
    pg_db.commit()

    result = retry_task(pg_db, log, _mock_dispatcher())

    assert isinstance(result, RetryResult)
    assert result.success is False
    assert result.error_code == 4004
    assert "failed/timeout/cancelled" in result.error_message


def test_retry_detects_duplicate_active_task(pg_db: Session) -> None:
    source = VideoSource(
        source_name="cam1",
        camera_name="cam1",
        location_name="door",
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

    result = retry_task(pg_db, failed, _mock_dispatcher())

    assert result.success is False
    assert result.error_code == 4004
    assert "already running" in result.error_message


def test_retry_session_build(pg_db: Session) -> None:
    source = VideoSource(
        source_name="cam1",
        camera_name="cam1",
        location_name="door",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
        last_validate_status="success",
    )
    pg_db.add(source)
    pg_db.flush()

    log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source.id,
        status=TaskStatus.FAILED,
        detail_json={"scan_mode": "hot"},
    )
    pg_db.add(log)
    pg_db.commit()

    dispatcher = _mock_dispatcher()
    result = retry_task(pg_db, log, dispatcher)

    assert result.success is True
    assert result.task_id == "build-task-1"
    dispatcher.dispatch_session_build.assert_called_once()


def test_retry_session_analysis_resets_to_sealed(pg_db: Session) -> None:
    source = VideoSource(
        source_name="cam1",
        camera_name="cam1",
        location_name="door",
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

    log = TaskLog(
        task_type=TaskType.SESSION_ANALYSIS,
        task_target_id=session.id,
        status=TaskStatus.FAILED,
        detail_json={"priority": "full"},
    )
    pg_db.add(log)
    pg_db.commit()

    dispatcher = _mock_dispatcher()
    result = retry_task(pg_db, log, dispatcher)

    assert result.success is True
    assert result.task_id == "analysis-task-1"
    pg_db.refresh(session)
    assert session.analysis_status == SessionAnalysisStatus.SEALED
    dispatcher.dispatch_analyze_session.assert_called_once()


def test_retry_daily_summary(pg_db: Session) -> None:
    log = TaskLog(
        task_type=TaskType.DAILY_SUMMARY_GENERATION,
        task_target_id=None,
        status=TaskStatus.FAILED,
        detail_json={"target_date": "2026-03-10"},
    )
    pg_db.add(log)
    pg_db.commit()

    dispatcher = _mock_dispatcher()
    result = retry_task(pg_db, log, dispatcher)

    assert result.success is True
    assert result.task_id == "summary-task-1"
    dispatcher.dispatch_generate_daily_summary.assert_called_once()


def test_retry_unsupported_task_type(pg_db: Session) -> None:
    log = TaskLog(
        task_type=TaskType.WEBHOOK_PUSH,
        task_target_id=None,
        status=TaskStatus.FAILED,
    )
    pg_db.add(log)
    pg_db.commit()

    result = retry_task(pg_db, log, _mock_dispatcher())

    assert result.success is False
    assert result.error_code == 4004
    assert "does not support retry" in result.error_message
