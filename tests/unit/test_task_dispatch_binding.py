from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.task_log import TaskLog
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.task_dispatch_control import (
    bind_or_create_running_task_log,
    build_dedupe_key,
    create_pending_task_log,
    record_deferred_hot_scan,
)


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TaskLog.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def test_bind_running_log_reuses_pending_by_dedupe_key() -> None:
    db = _new_db_session()
    try:
        detail = {"scan_mode": "hot", "source_id": 1}
        dedupe_key = build_dedupe_key(TaskType.SESSION_BUILD, 1, detail)
        detail["dedupe_key"] = dedupe_key
        pending, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json=detail,
        )
        assert created is True

        bound = bind_or_create_running_task_log(
            db,
            queue_task_id="queue-1",
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json={"scan_mode": "hot", "source_id": 1},
        )
        db.commit()

        assert bound.id == pending.id
        assert bound.status == TaskStatus.RUNNING
        assert bound.queue_task_id == "queue-1"
        assert bound.started_at is not None
    finally:
        db.close()


def test_bind_running_log_cancels_superseded_pending_candidates() -> None:
    db = _new_db_session()
    try:
        detail = {"scan_mode": "hot", "source_id": 1}
        dedupe_key = build_dedupe_key(TaskType.SESSION_BUILD, 1, detail)
        detail["dedupe_key"] = dedupe_key

        older, older_created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json=detail,
        )
        newer, newer_created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json=detail,
        )
        assert older_created is True
        assert newer_created is False

        bound = bind_or_create_running_task_log(
            db,
            queue_task_id="queue-2",
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json={"scan_mode": "hot", "source_id": 1},
        )
        db.commit()

        db.refresh(older)
        db.refresh(newer)
        assert bound.id == older.id
        assert older.status == TaskStatus.RUNNING
        assert newer.status == TaskStatus.RUNNING
        assert newer.message is None
    finally:
        db.close()


def test_session_build_dedupe_key_excludes_all_scan_modes_for_one_source() -> None:
    db = _new_db_session()
    try:
        full_log, full_created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json={"scan_mode": "full", "source_id": 1},
        )
        hot_log, hot_created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json={"scan_mode": "hot", "source_id": 1},
        )

        assert full_created is True
        assert hot_created is False
        assert hot_log.id == full_log.id
        assert full_log.dedupe_key == "source_scan:1"
    finally:
        db.close()


def test_hot_scan_deferred_behind_full_scan_records_reason() -> None:
    db = _new_db_session()
    try:
        full_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json={"scan_mode": "full", "source_id": 1},
        )
        assert created is True

        deferred = record_deferred_hot_scan(db, 1, full_log)
        db.commit()

        assert deferred.status == TaskStatus.SKIPPED
        assert deferred.message == "Deferred hot scan: full scan in progress for source 1"
        assert deferred.detail_json["reason"] == "full_scan_in_progress"
        assert deferred.detail_json["active_task_log_id"] == full_log.id
    finally:
        db.close()
