from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.maintenance import recover_timed_out_tasks
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType

pytestmark = pytest.mark.postgres


def test_concurrent_lost_analysis_recovery_creates_one_resume(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(postgres_migrated_engine) as db:
        source = VideoSource(
            source_name="lease-camera",
            camera_name="lease-camera",
            location_name="test",
            source_type="local_directory",
            config_json={"root_path": "/tmp/videos"},
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime.now(timezone.utc) - timedelta(minutes=30),
            session_end_time=datetime.now(timezone.utc) - timedelta(minutes=25),
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
        checkpoint = SessionAnalysisCheckpoint(
            session_id=session.id,
            analysis_run_id="a" * 64,
            chunk_index=0,
            sub_chunk_index=0,
            start_offset_seconds=0,
            input_fingerprint="b" * 64,
            state="success",
            event_payload={"events": []},
            completed_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db.add_all([task_log, checkpoint])
        db.commit()
        session_id = session.id

    local_session = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(
        "src.core.celery_app.celery_app.control.revoke", lambda *args, **kwargs: None
    )

    def recover() -> None:
        with local_session() as db:
            recover_timed_out_tasks(db, datetime.now(timezone.utc))
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(recover) for _ in range(2)]
        for future in futures:
            future.result()

    with Session(postgres_migrated_engine) as db:
        assert (
            db.query(func.count(TaskLog.id))
            .filter(
                TaskLog.task_type == TaskType.SESSION_ANALYSIS,
                TaskLog.task_target_id == session_id,
                TaskLog.status == TaskStatus.TIMEOUT,
            )
            .scalar()
            == 1
        )
        assert (
            db.query(func.count(TaskLog.id))
            .filter(
                TaskLog.task_type == TaskType.SESSION_ANALYSIS,
                TaskLog.task_target_id == session_id,
                TaskLog.status == TaskStatus.PENDING,
            )
            .scalar()
            == 1
        )
        assert db.query(SessionAnalysisCheckpoint).filter_by(session_id=session_id).count() == 1
