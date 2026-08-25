from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.application.pipeline.commands import SessionBuildCommand
from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
from src.models.task_log import TaskLog
from src.services.pipeline_constants import ScanMode, TaskStatus, TaskType
from src.services.task_dispatch_control import create_pending_task_log


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TaskLog.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def test_full_dispatch_supersedes_active_hot_and_enqueues_full(monkeypatch) -> None:
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
        monkeypatch.setattr("src.infrastructure.tasks.celery_dispatcher.SessionLocal", lambda: db)
        monkeypatch.setattr(
            "src.infrastructure.tasks.celery_dispatcher.celery_app.send_task",
            lambda *args, **kwargs: SimpleNamespace(id="full-task-id"),
        )

        task_id = CeleryTaskDispatcher().dispatch_session_build(
            SessionBuildCommand(source_id=1, scan_mode=ScanMode.FULL)
        )

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
        assert task_id == "full-task-id"
        assert hot_log.status == TaskStatus.CANCELLED
        assert hot_log.message == "Superseded by full scan request for source 1"
        assert full_log.detail_json["scan_mode"] == ScanMode.FULL
        assert full_log.queue_task_id == "full-task-id"
    finally:
        db.close()
