from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401
from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.registry import OutboxCommandRegistry
from src.application.pipeline.commands import SessionBuildCommand
from src.db.base_class import Base
from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog
from src.services.pipeline_constants import ScanMode, TaskStatus, TaskType
from src.services.task_dispatch_control import create_pending_task_log


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def test_full_dispatch_supersedes_active_hot_and_enqueues_full() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()
    OutboxCommandRegistry.reset_for_testing()
    db = _new_db_session()
    try:
        hot_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json={"scan_mode": ScanMode.HOT, "source_id": 1},
        )
        assert created is True
        db.commit()
        hot_log_id = hot_log.id

        task_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=1, scan_mode=ScanMode.FULL),
        )
        db.commit()

        hot_log = db.query(TaskLog).filter(TaskLog.id == hot_log_id).one()
        full_log = (
            db.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == 1,
                TaskLog.status == TaskStatus.PENDING,
            )
            .one()
        )
        outbox_row = (
            db.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == full_log.id).one()
        )
        assert task_id == str(outbox_row.event_id)
        assert hot_log.status == TaskStatus.CANCELLED
        assert hot_log.message == "Superseded by full scan request for source 1"
        assert full_log.detail_json["scan_mode"] == ScanMode.FULL
        assert full_log.queue_task_id == str(outbox_row.event_id)
    finally:
        db.close()
