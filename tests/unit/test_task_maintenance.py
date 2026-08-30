"""Test task maintenance heartbeat.

These tests are simplified since the old reconcile_pipeline_tasks has been
replaced by the heartbeat task with different responsibilities.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.tasks.task_maintenance import _recover_orphan_pending_tasks, _recover_timed_out_tasks


def _new_db_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSession.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    SessionAnalysisCheckpoint.__table__.create(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def test_worker_loss_after_checkpoint_auto_resumes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = _new_db_session_factory()
    db: Session = session_factory()
    try:
        session = VideoSession(
            source_id=1,
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
        dispatcher = MagicMock()
        monkeypatch.setattr("src.tasks.task_maintenance.CeleryTaskDispatcher", lambda: dispatcher)
        monkeypatch.setattr("src.tasks.task_maintenance.celery_app.control.revoke", MagicMock())

        now = datetime.now(timezone.utc)
        count = _recover_timed_out_tasks(db, now)
        db.commit()

        assert count == 1
        db.refresh(task_log)
        db.refresh(session)
        assert task_log.status == TaskStatus.TIMEOUT
        assert session.analysis_status == SessionAnalysisStatus.SEALED
        assert dispatcher.dispatch_analyze_session.call_count == 1

        assert _recover_timed_out_tasks(db, now) == 0
        assert dispatcher.dispatch_analyze_session.call_count == 1
    finally:
        db.close()


def test_healthy_queued_pending_task_is_not_falsely_timed_out() -> None:
    session_factory = _new_db_session_factory()
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
) -> None:
    session_factory = _new_db_session_factory()
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


def test_unleased_stale_analysis_task_is_not_touched() -> None:
    session_factory = _new_db_session_factory()
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
) -> None:
    session_factory = _new_db_session_factory()
    db: Session = session_factory()
    try:
        session = VideoSession(
            source_id=1,
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
        dispatcher = MagicMock()
        monkeypatch.setattr("src.tasks.task_maintenance.CeleryTaskDispatcher", lambda: dispatcher)

        assert _recover_timed_out_tasks(db, datetime.now(timezone.utc)) == 0
        assert dispatcher.dispatch_analyze_session.call_count == 0
    finally:
        db.close()


def test_exhausted_recovery_budget_records_terminal_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = _new_db_session_factory()
    db: Session = session_factory()
    try:
        session = VideoSession(
            source_id=1,
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
        dispatcher = MagicMock()
        monkeypatch.setattr("src.tasks.task_maintenance.CeleryTaskDispatcher", lambda: dispatcher)

        assert _recover_timed_out_tasks(db, datetime.now(timezone.utc)) == 1
        db.commit()

        db.refresh(task_log)
        assert task_log.status == TaskStatus.TIMEOUT
        assert task_log.message == f"Automatic recovery limit reached for session {session.id}"
        assert dispatcher.dispatch_analyze_session.call_count == 0
    finally:
        db.close()
