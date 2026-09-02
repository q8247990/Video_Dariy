from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.application.pipeline.commands import SessionBuildCommand
from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.session_builder import SessionBuilder
from src.services.task_dispatch_control import (
    bind_or_create_running_task_log,
    create_pending_task_log,
    record_deferred_hot_scan,
)

pytestmark = pytest.mark.postgres


def _claim_source_scan(engine: Engine, scan_mode: str) -> tuple[int, bool]:
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with local_session() as db:
        task_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=101,
            detail_json={"scan_mode": scan_mode, "source_id": 101},
        )
        db.commit()
        return task_log.id, created


def test_concurrent_source_scan_claims_create_one_active_build(
    postgres_migrated_engine: Engine,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda mode: _claim_source_scan(postgres_migrated_engine, mode),
                ["hot", "hot"],
            )
        )

    with Session(postgres_migrated_engine) as db:
        active_count = (
            db.query(func.count(TaskLog.id))
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == 101,
                TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
            )
            .scalar()
        )

    assert sum(created for _, created in claims) == 1
    assert active_count == 1


def test_hot_collision_records_recoverable_deferred_task_log(
    postgres_migrated_engine: Engine,
) -> None:
    with Session(postgres_migrated_engine) as db:
        full_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=202,
            detail_json={"scan_mode": "full", "source_id": 202},
        )
        assert created is True
        duplicate, duplicate_created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=202,
            detail_json={"scan_mode": "hot", "source_id": 202},
        )
        assert duplicate_created is False
        deferred = record_deferred_hot_scan(db, 202, duplicate)
        db.commit()

        assert deferred.status == TaskStatus.SKIPPED
        assert deferred.detail_json["reason"] == "full_scan_in_progress"
        assert deferred.detail_json["active_task_log_id"] == full_log.id


def test_full_dispatch_supersedes_hot_and_enqueues_full_on_postgres(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_session = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    with local_session() as db:
        hot_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=303,
            detail_json={"scan_mode": "hot", "source_id": 303},
        )
        assert created is True
        db.commit()
        hot_log_id = hot_log.id

    monkeypatch.setattr("src.infrastructure.tasks.celery_dispatcher.SessionLocal", local_session)
    monkeypatch.setattr(
        "src.infrastructure.tasks.celery_dispatcher.celery_app.send_task",
        lambda *args, **kwargs: type("Task", (), {"id": "postgres-full-task"})(),
    )

    task_id = CeleryTaskDispatcher().dispatch_session_build(
        SessionBuildCommand(source_id=303, scan_mode="full")
    )

    with Session(postgres_migrated_engine) as db:
        hot_log = db.query(TaskLog).filter(TaskLog.id == hot_log_id).one()
        full_log = (
            db.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == 303,
                TaskLog.status == TaskStatus.PENDING,
            )
            .one()
        )
        assert task_id == "postgres-full-task"
        assert hot_log.status == TaskStatus.CANCELLED
        assert full_log.detail_json["scan_mode"] == "full"


def test_full_and_hot_build_collision_preserves_one_file_and_relation(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(postgres_migrated_engine) as db:
        source = VideoSource(
            source_name="concurrent-camera",
            camera_name="concurrent-camera",
            location_name="test",
            source_type="local_directory",
            config_json={"root_path": "/tmp/videos"},
            enabled=True,
        )
        db.add(source)
        db.commit()
        source_id = source.id

    start_time = datetime(2026, 3, 15, 9, 4, 4, tzinfo=timezone.utc)
    records = [
        {
            "file_name": "concurrent.mp4",
            "file_path": "/tmp/concurrent.mp4",
            "start_time": start_time,
            "end_time": start_time + timedelta(seconds=60),
            "duration_seconds": 60,
            "file_size": 1024,
            "file_format": "mp4",
            "storage_type": "local_file",
        }
    ]
    monkeypatch.setattr(
        "src.services.session_builder.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time=None, max_time=None, cancel_check=None: records,
    )

    def build(scan_mode: str) -> None:
        with Session(postgres_migrated_engine) as db:
            SessionBuilder().build(
                db=db,
                source_id=source_id,
                root_path="/tmp/videos",
                scan_mode=scan_mode,
                scan_start=start_time - timedelta(hours=1),
                scan_end=start_time + timedelta(hours=1),
            )
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(build, scan_mode) for scan_mode in ("full", "hot")]
        for future in futures:
            future.result()

    with Session(postgres_migrated_engine) as db:
        assert db.query(VideoFile).filter(VideoFile.source_id == source_id).count() == 1
        assert db.query(VideoSession).filter(VideoSession.source_id == source_id).count() == 1
        session_id = db.query(VideoSession.id).filter(VideoSession.source_id == source_id).one()[0]
        assert (
            db.query(VideoSessionFileRel)
            .filter(VideoSessionFileRel.session_id == session_id)
            .count()
            == 1
        )


def test_stale_queue_message_for_timed_out_row_binds_to_none_on_postgres(
    postgres_migrated_engine: Engine,
) -> None:
    local_session = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    with local_session() as db:
        stale_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=404,
            detail_json={"scan_mode": "hot", "source_id": 404},
        )
        assert created is True
        stale_log.queue_task_id = "queue-old"
        db.commit()
        stale_log_id = stale_log.id

    with local_session() as db:
        stale_row = db.query(TaskLog).filter(TaskLog.id == stale_log_id).one()
        stale_row.status = TaskStatus.TIMEOUT
        stale_row.finished_at = datetime.now(timezone.utc)
        stale_row.message = "Pending task was never picked up by a worker; timed out"
        db.commit()

    with local_session() as db:
        bound = bind_or_create_running_task_log(
            db,
            queue_task_id="queue-old",
            task_type=TaskType.SESSION_BUILD,
            task_target_id=404,
            detail_json={"scan_mode": "hot", "source_id": 404},
        )
        db.commit()

        assert bound is None
        stale_row = db.query(TaskLog).filter(TaskLog.id == stale_log_id).one()
        assert stale_row.status == TaskStatus.TIMEOUT
        assert (
            db.query(TaskLog)
            .filter(TaskLog.task_target_id == 404, TaskLog.id != stale_log_id)
            .count()
            == 0
        )


def test_stale_message_insert_collision_returns_none_on_postgres(
    postgres_migrated_engine: Engine,
) -> None:
    local_session = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    with local_session() as db:
        active_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=505,
            detail_json={"scan_mode": "hot", "source_id": 505},
        )
        assert created is True
        active_log.queue_task_id = "queue-active"
        db.commit()
        active_log_id = active_log.id

    with local_session() as db:
        active_row = db.query(TaskLog).filter(TaskLog.id == active_log_id).one()
        active_row.status = TaskStatus.RUNNING
        active_row.started_at = datetime.now(timezone.utc)
        db.commit()

    with local_session() as db:
        bound = bind_or_create_running_task_log(
            db,
            queue_task_id="queue-stale",
            task_type=TaskType.SESSION_BUILD,
            task_target_id=505,
            detail_json={"scan_mode": "hot", "source_id": 505},
        )
        db.commit()

        assert bound is None
        assert db.query(TaskLog).filter(TaskLog.queue_task_id == "queue-stale").count() == 0
