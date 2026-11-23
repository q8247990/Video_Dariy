"""Dispatch intents are collected per execution context and flushed after commit.

The dispatcher used to keep a process-wide ``pending_deliveries`` list and
the heartbeat popped it. That mixed concurrent API requests and delayed
every dispatch until the next heartbeat (up to 60 s). Intents are now
tracked in the :func:`src.services.dispatch.outbox.pending_deliveries`
context and published by the caller immediately after its commit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.models.task_delivery import TaskDelivery
from src.models.task_log import TaskLog
from src.services.dispatch.outbox import (
    flush_task_deliveries,
    pending_deliveries,
    record_delivery,
)
from src.services.dispatch.task_command import TaskCommand


def _stage_one(db: Session) -> Any:
    task_log = TaskLog(task_type="session_build", status="pending")
    db.add(task_log)
    db.flush()
    return record_delivery(
        db,
        task_log,
        TaskCommand(
            task_name="src.tasks.session_build.full_build_task",
            queue="celery",
            args=(7,),
        ),
        task_id="immediate-flush-task-id",
    )


def test_record_delivery_is_collected_in_active_context(pg_db: Session) -> None:
    task_log = TaskLog(task_type="session_build", status="pending")
    pg_db.add(task_log)
    pg_db.flush()

    with pending_deliveries() as deliveries:
        record_delivery(
            pg_db,
            task_log,
            TaskCommand(
                task_name="src.tasks.session_build.full_build_task",
                queue="celery",
                args=(7,),
            ),
            task_id="immediate-flush-task-id",
        )
        assert len(deliveries) == 1
        assert deliveries[0].task_log_id == task_log.id
        assert task_log.queue_task_id == "immediate-flush-task-id"


def test_flush_publishes_recorded_intents_and_clears_rows(pg_db: Session, monkeypatch: Any) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.services.dispatch.outbox.celery_app.send_task",
        lambda *args, **kwargs: sent.append({"args": args, "kwargs": kwargs}),
    )

    with pending_deliveries() as deliveries:
        _stage_one(pg_db)
    pg_db.flush()

    assert len(deliveries) == 1
    assert flush_task_deliveries(pg_db, deliveries) == 1
    assert len(sent) == 1
    assert sent[0]["kwargs"]["task_id"] == "immediate-flush-task-id"
    assert sent[0]["kwargs"]["queue"] == "celery"
    assert pg_db.query(TaskDelivery).count() == 0
