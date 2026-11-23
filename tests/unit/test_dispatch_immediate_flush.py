"""Dispatch intents are collected per execution context and flushed after commit.

The dispatcher used to keep a process-wide ``pending_deliveries`` list and
the heartbeat popped it. That mixed concurrent API requests and delayed
every dispatch until the next heartbeat (up to 60 s). Intents are now
tracked in the :func:`src.services.dispatch.outbox.pending_deliveries`
context and published by the caller immediately after its commit.

The context collects session-independent :class:`DeliveryIntent` handles
(just ``task_log_id``) rather than ORM instances: Celery handlers routinely
commit and close the recording session before flushing in a fresh one, and
an expired instance from a closed session raises ``DetachedInstanceError``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.task_delivery import TaskDelivery
from src.models.task_log import TaskLog
from src.services.dispatch.outbox import (
    DeliveryIntent,
    flush_task_deliveries,
    pending_deliveries,
    record_delivery,
)
from src.services.dispatch.task_command import TaskCommand

SessionFactory = Callable[[], Session]


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
        assert isinstance(deliveries[0], DeliveryIntent)
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


def test_flush_publishes_intents_after_recording_session_is_closed(
    pg_db_factory: SessionFactory, monkeypatch: Any
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.services.dispatch.outbox.celery_app.send_task",
        lambda *args, **kwargs: sent.append({"args": args, "kwargs": kwargs}),
    )

    recording = pg_db_factory()
    try:
        with pending_deliveries() as deliveries:
            task_log_id = int(_stage_one(recording).task_log_id)
        recording.commit()
    finally:
        recording.close()

    assert [intent.task_log_id for intent in deliveries] == [task_log_id]

    publishing = pg_db_factory()
    try:
        assert flush_task_deliveries(publishing, deliveries) == 1
        assert publishing.query(TaskDelivery).count() == 0
    finally:
        publishing.close()

    assert len(sent) == 1
    assert sent[0]["kwargs"]["task_id"] == "immediate-flush-task-id"
    assert sent[0]["kwargs"]["queue"] == "celery"


def test_flush_skips_intent_already_published_by_sweep(
    pg_db_factory: SessionFactory, monkeypatch: Any
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.services.dispatch.outbox.celery_app.send_task",
        lambda *args, **kwargs: sent.append({"args": args, "kwargs": kwargs}),
    )

    recording = pg_db_factory()
    try:
        with pending_deliveries() as deliveries:
            _stage_one(recording)
        recording.commit()
    finally:
        recording.close()

    publishing = pg_db_factory()
    try:
        assert publishing.query(TaskDelivery).delete() == 1
        publishing.commit()
        assert flush_task_deliveries(publishing, deliveries) == 0
    finally:
        publishing.close()

    assert sent == []


def test_flush_skips_intent_locked_by_concurrent_sweep(
    pg_db_factory: SessionFactory, monkeypatch: Any
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.services.dispatch.outbox.celery_app.send_task",
        lambda *args, **kwargs: sent.append({"args": args, "kwargs": kwargs}),
    )

    recording = pg_db_factory()
    try:
        with pending_deliveries() as deliveries:
            _stage_one(recording)
        recording.commit()
    finally:
        recording.close()

    sweeping = pg_db_factory()
    publishing = pg_db_factory()
    try:
        locked = sweeping.execute(select(TaskDelivery).with_for_update()).scalars().all()
        assert len(locked) == 1
        assert flush_task_deliveries(publishing, deliveries) == 0
        assert sweeping.query(TaskDelivery).count() == 1
    finally:
        sweeping.rollback()
        sweeping.close()
        publishing.close()

    assert sent == []
