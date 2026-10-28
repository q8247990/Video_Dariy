"""Broker delivery for dispatch intents (replaces the outbox publisher).

The dispatcher records one :class:`~src.models.task_delivery.TaskDelivery`
row per ``TaskLog`` inside the caller's transaction and pre-assigns the
broker ``task_id`` to ``TaskLog.queue_task_id``. After the caller commits,
:func:`flush_task_deliveries` sends the recorded intents; the heartbeat
retries rows whose last attempt failed via
:func:`retry_failed_deliveries`.

Delivery semantics stay **at-least-once**: a crash between commit and
``send_task`` leaves the intent row behind, and the retry sweep re-sends it
with the same ``task_id`` so the consumer-side
``bind_or_create_running_task_log`` idempotency seam collapses the
duplicate.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import inspect, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.core.celery_app import celery_app
from src.models.task_delivery import TaskDelivery
from src.models.task_log import TaskLog
from src.services.dispatch.payload import ensure_json_safe
from src.services.dispatch.task_command import TaskCommand

logger = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 5
RETIRED_OUTBOX_TASK_NAMES = frozenset(
    {
        "src.tasks.session_build.full_build_task",
        "src.tasks.session_build.hot_build_task",
        "src.tasks.analyzer.analyze_session_task",
        "src.tasks.summarizer.generate_daily_summary_task",
        "src.tasks.webhook.send_webhook_task",
    }
)


_PENDING: "ContextVar[list[TaskDelivery] | None]" = ContextVar("pending_deliveries", default=None)


@contextmanager
def _pending_deliveries_impl() -> Iterator[list[TaskDelivery]]:
    deliveries: list[TaskDelivery] = []
    token = _PENDING.set(deliveries)
    try:
        yield deliveries
    finally:
        _PENDING.reset(token)


def pending_deliveries() -> AbstractContextManager[list[TaskDelivery]]:
    """Collect the intents recorded inside a ``with`` block.

    Used by task handlers: ``with pending_deliveries() as recorded:`` gathers
    every intent the dispatcher staged during the block so the handler can
    flush them after its own commit.
    """
    return _pending_deliveries_impl()


def track_delivery(delivery: TaskDelivery) -> None:
    """Register ``delivery`` with the active :func:`pending_deliveries` block."""
    active = _PENDING.get()
    if active is not None:
        active.append(delivery)


def new_task_id() -> str:
    """Return the broker ``task_id`` for a dispatch about to be recorded."""
    return str(uuid4())


def record_delivery(
    db: Session,
    task_log: TaskLog,
    command: TaskCommand,
    *,
    task_id: str,
) -> TaskDelivery:
    """Stage the delivery of ``command`` for ``task_log`` in the caller's tx."""
    ensure_json_safe({"args": list(command.args), "kwargs": dict(command.kwargs or {})})
    delivery = TaskDelivery(
        task_log_id=int(task_log.id),
        task_name=command.task_name,
        queue=command.queue,
        args_json=list(command.args),
        kwargs_json=dict(command.kwargs or {}),
        attempt_count=0,
    )
    task_log.queue_task_id = task_id
    task_log.message = "Queued (delivery recorded)"
    db.add(delivery)
    track_delivery(delivery)
    return delivery


def send_delivery(delivery: TaskDelivery, task_id: str) -> None:
    """Push one recorded intent to the broker."""
    celery_app.send_task(
        delivery.task_name,
        args=list(delivery.args_json),
        kwargs=dict(delivery.kwargs_json),
        queue=delivery.queue,
        task_id=task_id,
    )


def _failure_result(delivery: TaskDelivery, exc: Exception) -> bool:
    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
    delivery.last_error = f"{type(exc).__name__}: {exc}"[:1024]
    logger.warning(
        "Task delivery failed task_log_id=%s task_name=%s attempt=%s error=%s",
        delivery.task_log_id,
        delivery.task_name,
        delivery.attempt_count,
        delivery.last_error,
    )
    return False


def flush_task_deliveries(db: Session, deliveries: list[TaskDelivery]) -> int:
    """Send ``deliveries`` and drop the ones that reached the broker.

    Call **after** the caller committed the surrounding transaction. Returns
    the number of intents the broker accepted; the failures stay in
    ``task_delivery`` for :func:`retry_failed_deliveries`.
    """
    if not deliveries:
        return 0
    sent = 0
    for delivery in deliveries:
        task_log = db.get(TaskLog, delivery.task_log_id)
        task_id = str(task_log.queue_task_id) if task_log is not None else None
        if task_id is None:
            task_id = new_task_id()
        try:
            send_delivery(delivery, task_id)
        except Exception as exc:
            _failure_result(delivery, exc)
            continue
        db.delete(delivery)
        sent += 1
    db.commit()
    return sent


def retry_failed_deliveries(db: Session, *, limit: int = 100) -> int:
    """Re-send intents whose previous attempt failed; drop the exhausted ones."""
    stale = (
        db.execute(
            select(TaskDelivery)
            .where(
                or_(
                    TaskDelivery.last_error.is_not(None),
                    TaskDelivery.last_attempt_at.is_(None),
                )
            )
            .order_by(TaskDelivery.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    resent = 0
    for delivery in stale:
        if int(delivery.attempt_count or 0) >= MAX_DELIVERY_ATTEMPTS:
            logger.error(
                "Task delivery abandoned task_log_id=%s task_name=%s attempts=%s error=%s",
                delivery.task_log_id,
                delivery.task_name,
                delivery.attempt_count,
                delivery.last_error,
            )
            db.delete(delivery)
            continue
        task_log = db.get(TaskLog, delivery.task_log_id)
        task_id = str(task_log.queue_task_id) if task_log is not None else None
        try:
            send_delivery(delivery, task_id or new_task_id())
        except Exception as exc:
            _failure_result(delivery, exc)
            continue
        db.delete(delivery)
        resent += 1
    db.commit()
    return resent


def replay_retired_outbox(db: Session, *, limit: int = 500) -> int:
    """One-shot replay of ``outbox_event`` rows the retired publisher left behind.

    ADR 0011 kept ``outbox_event`` because its migration is irreversible. A
    deployment upgraded to ADR 0013 may still hold rows the old publisher
    never sent; they are re-sent here with ``task_id = event_id`` so the
    consumer binds them exactly as before, and then deleted because the
    stand-alone publisher no longer writes that table.

    Returns the number of rows replayed.
    """
    bind = db.get_bind()
    if not isinstance(bind, Engine) or not inspect(bind).has_table("outbox_event"):
        return 0
    rows: list[Any] = list(
        db.execute(
            select(_outbox_row_type())
            .where(_outbox_pending_condition())
            .order_by(_outbox_id_column().asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    replayed = 0
    for row in rows:
        try:
            celery_app.send_task(
                row.task_name,
                args=list(row.args_json or []),
                kwargs=dict(row.kwargs_json or {}),
                queue=row.queue,
                task_id=str(row.event_id),
            )
        except Exception:
            logger.exception(
                "Retired-outbox replay failed event_id=%s task_name=%s",
                row.event_id,
                row.task_name,
            )
            continue
        db.delete(row)
        replayed += 1
    db.commit()
    return replayed


def _outbox_row_type() -> Any:
    from src.models.outbox import OutboxEvent

    return OutboxEvent


def _outbox_pending_condition() -> Any:
    from src.models.outbox import OutboxEvent

    return OutboxEvent.status.in_(("pending", "failed"))


def _outbox_id_column() -> Any:
    from src.models.outbox import OutboxEvent

    return OutboxEvent.id


def task_id_for(task_log: TaskLog) -> str:
    """Return the broker ``task_id`` already assigned to ``task_log``."""
    if task_log.queue_task_id:
        return str(task_log.queue_task_id)
    task_id = new_task_id()
    task_log.queue_task_id = task_id
    return task_id


def as_uuid(value: str) -> UUID:
    return UUID(value)


__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "RETIRED_OUTBOX_TASK_NAMES",
    "TaskCommand",
    "as_uuid",
    "flush_task_deliveries",
    "new_task_id",
    "record_delivery",
    "replay_retired_outbox",
    "retry_failed_deliveries",
    "pending_deliveries",
    "send_delivery",
    "task_id_for",
    "track_delivery",
]
