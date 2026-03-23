"""Test task maintenance heartbeat.

These tests are simplified since the old reconcile_pipeline_tasks has been
replaced by the heartbeat task with different responsibilities.
"""

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.tasks.task_maintenance import _recover_orphan_pending_tasks, _recover_timed_out_tasks


def _new_db_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TaskLog.__table__.create(bind=engine)
    VideoSession.__table__.create(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def test_recover_timed_out_analysis_resets_session_to_sealed() -> None:
    session_factory = _new_db_session_factory()
    db: Session = session_factory()
    try:
        session = VideoSession(
            source_id=1,
            session_start_time=datetime(2026, 3, 10, 8, 0, 0),
            session_end_time=datetime(2026, 3, 10, 8, 5, 0),
            total_duration_seconds=300,
            analysis_status=SessionAnalysisStatus.ANALYZING,
        )
        db.add(session)
        db.flush()

        task_log = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session.id,
            status=TaskStatus.RUNNING,
            started_at=datetime.now() - timedelta(hours=2),
        )
        db.add(task_log)
        db.commit()

        now = datetime.now()
        count = _recover_timed_out_tasks(db, now)
        db.commit()

        assert count == 1
        db.refresh(task_log)
        db.refresh(session)
        assert task_log.status == TaskStatus.TIMEOUT
        assert session.analysis_status == SessionAnalysisStatus.SEALED
    finally:
        db.close()


def test_recover_orphan_pending_marks_timeout() -> None:
    session_factory = _new_db_session_factory()
    db: Session = session_factory()
    try:
        pending = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            status=TaskStatus.PENDING,
            detail_json={"scan_mode": "hot"},
        )
        pending.created_at = datetime.now() - timedelta(minutes=10)
        db.add(pending)
        db.commit()

        now = datetime.now()
        count = _recover_orphan_pending_tasks(db, now)
        db.commit()

        assert count == 1
        db.refresh(pending)
        assert pending.status == TaskStatus.TIMEOUT
        assert pending.message == "Pending task orphaned"
    finally:
        db.close()
