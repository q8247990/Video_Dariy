"""Test task maintenance heartbeat.

These tests are simplified since the old reconcile_pipeline_tasks has been
replaced by the heartbeat task with different responsibilities.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeTaskDispatcher
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.tasks.task_maintenance import (
    _mark_missing_video_files,
    _recover_orphan_pending_tasks,
    _recover_timed_out_tasks,
)

SessionFactory = Callable[[], Session]


def _bind_fake_dispatcher(monkeypatch: pytest.MonkeyPatch) -> FakeTaskDispatcher:
    dispatcher = FakeTaskDispatcher()
    container = bootstrap_for_tests(dispatcher=dispatcher)
    monkeypatch.setattr("src.tasks.task_maintenance.get_container", lambda: container)
    return dispatcher


def test_worker_loss_after_checkpoint_auto_resumes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db: Session = session_factory()
    try:
        source = VideoSource(
            source_name="cam",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 10, 8, 0, 0, tzinfo=timezone.utc),
            session_end_time=datetime(2026, 3, 10, 8, 5, 0, tzinfo=timezone.utc),
            total_duration_seconds=300,
            analysis_status=SessionAnalysisStatus.ANALYZING,
        )
        db.add(session)
        db.flush()

        task_log = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session.id,
            status=TaskStatus.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            last_heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(task_log)
        db.add(
            SessionAnalysisCheckpoint(
                session_id=session.id,
                analysis_run_id="run",
                chunk_index=0,
                sub_chunk_index=0,
                start_offset_seconds=0,
                input_fingerprint="a" * 64,
                state="success",
                completed_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        db.commit()
        dispatcher = _bind_fake_dispatcher(monkeypatch)
        monkeypatch.setattr("src.tasks.task_maintenance.celery_app.control.revoke", MagicMock())

        now = datetime.now(timezone.utc)
        count = _recover_timed_out_tasks(db, now)
        db.commit()

        assert count == 1
        db.refresh(task_log)
        db.refresh(session)
        assert task_log.status == TaskStatus.TIMEOUT
        assert session.analysis_status == SessionAnalysisStatus.SEALED
        assert len(dispatcher.dispatched_analyze_session) == 1

        assert _recover_timed_out_tasks(db, now) == 0
        assert len(dispatcher.dispatched_analyze_session) == 1
    finally:
        db.close()


def test_healthy_queued_pending_task_is_not_falsely_timed_out(
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db: Session = session_factory()
    try:
        pending = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            status=TaskStatus.PENDING,
            detail_json={"scan_mode": "hot"},
        )
        pending.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.add(pending)
        db.commit()

        now = datetime.now(timezone.utc)
        count = _recover_orphan_pending_tasks(db, now)
        db.commit()

        assert count == 0
        db.refresh(pending)
        assert pending.status == TaskStatus.PENDING
    finally:
        db.close()


def test_unleased_stale_build_task_times_out_and_revokes(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db: Session = session_factory()
    try:
        pending = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            status=TaskStatus.PENDING,
            detail_json={"scan_mode": "full"},
            queue_task_id="lost-celery-task-id",
        )
        pending.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.add(pending)
        db.commit()

        revoke_mock = MagicMock()
        monkeypatch.setattr("src.tasks.task_maintenance.celery_app.control.revoke", revoke_mock)

        now = datetime.now(timezone.utc)
        count = _recover_orphan_pending_tasks(db, now)
        db.commit()

        assert count == 1
        db.refresh(pending)
        assert pending.status == TaskStatus.TIMEOUT
        assert pending.message == "Pending task was never picked up by a worker; timed out"
        revoke_mock.assert_called_once_with("lost-celery-task-id", terminate=True)

        assert _recover_orphan_pending_tasks(db, now) == 0
    finally:
        db.close()


def test_unleased_stale_analysis_task_is_not_touched(
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db: Session = session_factory()
    try:
        pending = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=1,
            status=TaskStatus.PENDING,
            detail_json={"priority": "hot"},
            queue_task_id="queued-celery-task-id",
        )
        pending.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.add(pending)
        db.commit()

        now = datetime.now(timezone.utc)
        count = _recover_orphan_pending_tasks(db, now)
        db.commit()

        assert count == 0
        db.refresh(pending)
        assert pending.status == TaskStatus.PENDING
    finally:
        db.close()


def test_cancelled_analysis_is_not_auto_resumed(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db: Session = session_factory()
    try:
        source = VideoSource(
            source_name="cam",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime.now(timezone.utc),
            session_end_time=datetime.now(timezone.utc),
            analysis_status=SessionAnalysisStatus.ANALYZING,
        )
        db.add(session)
        db.flush()
        task_log = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session.id,
            status=TaskStatus.RUNNING,
            cancel_requested=True,
            recovery_attempt=0,
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(task_log)
        db.commit()
        dispatcher = _bind_fake_dispatcher(monkeypatch)

        assert _recover_timed_out_tasks(db, datetime.now(timezone.utc)) == 0
        assert len(dispatcher.dispatched_analyze_session) == 0
    finally:
        db.close()


def test_exhausted_recovery_budget_records_terminal_reason(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db: Session = session_factory()
    try:
        source = VideoSource(
            source_name="cam",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime.now(timezone.utc),
            session_end_time=datetime.now(timezone.utc),
            analysis_status=SessionAnalysisStatus.ANALYZING,
        )
        db.add(session)
        db.flush()
        task_log = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session.id,
            status=TaskStatus.RUNNING,
            recovery_attempt=3,
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(task_log)
        db.commit()
        dispatcher = _bind_fake_dispatcher(monkeypatch)

        assert _recover_timed_out_tasks(db, datetime.now(timezone.utc)) == 1
        db.commit()

        db.refresh(task_log)
        assert task_log.status == TaskStatus.TIMEOUT
        assert task_log.message == f"Automatic recovery limit reached for session {session.id}"
        assert len(dispatcher.dispatched_analyze_session) == 0
    finally:
        db.close()


def _make_source_with_files(db: Session, tmp_path, paused: bool = False):
    source = VideoSource(
        source_name="cam",
        camera_name="living",
        location_name="home",
        source_type="local_directory",
        enabled=True,
        source_paused=paused,
    )
    db.add(source)
    db.flush()
    (tmp_path / "2026010100").mkdir()
    present = tmp_path / "2026010100" / "present.mp4"
    present.write_bytes(b"video")
    present_file = VideoFile(
        source_id=source.id,
        file_name="present.mp4",
        file_path=str(present),
        start_time=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
    )
    missing_file = VideoFile(
        source_id=source.id,
        file_name="gone.mp4",
        file_path=str(tmp_path / "2026010100" / "gone.mp4"),
        start_time=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 0, 2, 0, tzinfo=timezone.utc),
    )
    db.add_all([present_file, missing_file])
    db.commit()
    return source, present_file, missing_file


def test_mark_missing_video_files_sweeps_enabled_local_sources(
    tmp_path, monkeypatch: pytest.MonkeyPatch, pg_db_factory: SessionFactory
) -> None:
    monkeypatch.setattr("src.core.config.settings.VIDEO_ROOT_PATH", str(tmp_path))
    session_factory = pg_db_factory
    db: Session = session_factory()
    try:
        _, present_file, missing_file = _make_source_with_files(db, tmp_path)

        marked = _mark_missing_video_files(db)
        db.commit()
        db.refresh(present_file)
        db.refresh(missing_file)

        assert marked == 1
        assert present_file.file_missing is False
        assert missing_file.file_missing is True
    finally:
        db.close()


def test_mark_missing_video_files_skips_paused_sources(
    tmp_path, monkeypatch: pytest.MonkeyPatch, pg_db_factory: SessionFactory
) -> None:
    monkeypatch.setattr("src.core.config.settings.VIDEO_ROOT_PATH", str(tmp_path))
    session_factory = pg_db_factory
    db: Session = session_factory()
    try:
        _, _, missing_file = _make_source_with_files(db, tmp_path, paused=True)

        marked = _mark_missing_video_files(db)
        db.commit()
        db.refresh(missing_file)

        assert marked == 0
        assert missing_file.file_missing is False
    finally:
        db.close()
