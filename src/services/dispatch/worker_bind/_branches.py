"""Worker-bind branches: existing-row / pending-candidate / fresh-INSERT.

Each helper implements one of the three behavioural branches of
:func:`src.services.dispatch.worker_bind.bind_or_create_running_task_log`.
The orchestrator dispatches between them based on the
``queue_task_id`` and the candidate-scan results.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.services.dispatch.constants import TERMINAL_TASK_STATUSES
from src.services.dispatch.dedupe import ensure_dict_detail
from src.services.dispatch.worker_bind._state import (
    apply_running_state,
    mark_superseded_pending,
)
from src.services.pipeline_constants import TaskStatus


def bind_existing_queue_row(
    db: Session,
    *,
    existing: TaskLog,
    detail_payload: dict[str, Any],
    task_type: str,
    task_target_id: Optional[int],
    dedupe_key: str,
    queue_task_id: Optional[str],
    now: datetime,
    lease_seconds: int,
) -> Optional[TaskLog]:
    """Rebind a row whose ``queue_task_id`` matches the current broker message.

    Returns ``None`` when the row is already terminal (the "stale
    message" path — a redelivered broker message must never resurrect
    a finalized row). Otherwise returns the freshly-running row and
    supersedes any other pending candidates in the same transaction.
    """
    if existing.status in TERMINAL_TASK_STATUSES:
        # Stale queue message (its row was finalized, e.g. marked
        # timeout while the worker was down): never resurrect a
        # terminal row or insert a new one — a newer active row may
        # already hold this dedupe key.
        return None
    bound = apply_running_state(
        existing,
        detail_payload=detail_payload,
        task_type=task_type,
        task_target_id=task_target_id,
        dedupe_key=dedupe_key,
        queue_task_id=queue_task_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    mark_superseded_pending(
        db,
        bind_id=bound.id,
        task_type=task_type,
        task_target_id=task_target_id,
        dedupe_key=dedupe_key,
        now=now,
    )
    return bound


def bind_pending_candidate(
    db: Session,
    *,
    detail_payload: dict[str, Any],
    task_type: str,
    task_target_id: Optional[int],
    dedupe_key: str,
    queue_task_id: Optional[str],
    now: datetime,
    lease_seconds: int,
) -> Optional[TaskLog]:
    """Bind the latest PENDING TaskLog candidate that matches the dedupe-key.

    Returns ``None`` when no PENDING candidate matches; the caller
    then falls through to the fresh-INSERT branch.
    """
    pending_candidates = (
        db.query(TaskLog)
        .filter(TaskLog.task_type == task_type, TaskLog.status == TaskStatus.PENDING)
        .order_by(TaskLog.id.desc())
        .all()
    )
    for item in pending_candidates:
        if task_target_id is None and item.task_target_id is not None:
            continue
        if task_target_id is not None and item.task_target_id != task_target_id:
            continue
        item_detail = ensure_dict_detail(item.detail_json)
        if str(item_detail.get("dedupe_key") or "") != dedupe_key:
            continue
        bound = apply_running_state(
            item,
            detail_payload=detail_payload,
            task_type=task_type,
            task_target_id=task_target_id,
            dedupe_key=dedupe_key,
            queue_task_id=queue_task_id,
            now=now,
            lease_seconds=lease_seconds,
        )
        mark_superseded_pending(
            db,
            bind_id=bound.id,
            task_type=task_type,
            task_target_id=task_target_id,
            dedupe_key=dedupe_key,
            now=now,
        )
        return bound
    return None


def insert_fresh_running_row(
    db: Session,
    *,
    detail_payload: dict[str, Any],
    task_type: str,
    task_target_id: Optional[int],
    dedupe_key: str,
    queue_task_id: Optional[str],
    now: datetime,
    lease_seconds: int,
) -> Optional[TaskLog]:
    """INSERT a brand-new RUNNING TaskLog and return it (or ``None`` on race).

    PostgreSQL uses the partial unique index as the silent race net
    (``on_conflict_do_nothing`` returns ``inserted_id=None``).
    SQLite has no such index; we catch the resulting
    :class:`IntegrityError` and rollback the session.
    """
    row_values = {
        "task_type": task_type,
        "task_target_id": task_target_id,
        "dedupe_key": dedupe_key,
        "queue_task_id": queue_task_id,
        "status": TaskStatus.RUNNING,
        "started_at": now,
        "lease_owner": queue_task_id or None,
        "last_heartbeat_at": now,
        "lease_expires_at": now + timedelta(seconds=lease_seconds),
        "detail_json": detail_payload,
    }
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = (
            postgresql_insert(TaskLog)
            .values(**row_values)
            .on_conflict_do_nothing(
                index_elements=[TaskLog.dedupe_key],
                index_where=TaskLog.dedupe_key.is_not(None)
                & TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
            )
            .returning(TaskLog.id)
        )
        inserted_id = db.execute(stmt).scalar_one_or_none()
        db.flush()
        if inserted_id is None:
            return None
        return db.query(TaskLog).filter(TaskLog.id == inserted_id).one()

    task_log = TaskLog(**row_values)
    db.add(task_log)
    try:
        db.flush()
    except IntegrityError:
        # A newer active row claimed the same dedupe key between the
        # candidate scan and this insert: drop this binding instead of
        # crashing the worker with a UniqueViolation.
        db.rollback()
        return None
    return task_log


__all__ = [
    "bind_existing_queue_row",
    "bind_pending_candidate",
    "insert_fresh_running_row",
]
