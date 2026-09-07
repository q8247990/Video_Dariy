"""Claim policy — create the PENDING TaskLog row for new work.

The claim policy is the "no active row for this dedupe-key yet" branch
of :func:`src.services.dispatch.dedupe.find_duplicate_active_task`.
It returns the new (or pre-existing) TaskLog and a ``created`` flag so
the caller knows whether to enqueue the matching outbox event.

The PostgreSQL partial unique index
``ux_task_log_active_dedupe_key`` (created in the outbox cutover
migration) provides the cross-process concurrency safety net; under
SQLite (unit tests) the helper falls back to a SELECT-then-INSERT
path that the partial unique index would have caught.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from src.models.task_log import ACTIVE_DEDUPE_INDEX_PREDICATE, TaskLog
from src.services.dispatch.dedupe import (
    _ensure_dedupe_key,
    find_duplicate_active_task,
)
from src.services.pipeline_constants import TaskStatus


def create_pending_task_log(
    db: Session,
    task_type: str,
    task_target_id: Optional[int],
    detail_json: dict[str, Any],
) -> tuple[TaskLog, bool]:
    """Create the PENDING TaskLog row, or return the existing active row.

    Returns ``(task_log, created)``. ``created=True`` means the caller
    must enqueue a fresh outbox event for this work; ``created=False``
    means the partial unique index (or the legacy dedupe lookup) said
    "there is already an active row" and the caller's ``enqueue_command``
    call should be skipped — the existing row owns the publish intent.

    Args:
        db: Caller-owned SQLAlchemy session.
        task_type: :class:`src.services.pipeline_constants.TaskType`.
        task_target_id: ``VideoSource.id`` / ``VideoSession.id`` /
            ``None`` for webhook / summary rows.
        detail_json: Per-call metadata (scan mode, priority,
            target_date, …). The helper mutates this dict to embed
            ``dedupe_key`` when one was computed.

    Returns:
        ``(task_log, created)``. ``task_log`` is the row that owns the
        publish intent (newly-inserted or pre-existing).
    """
    detail_payload, dedupe_key = _ensure_dedupe_key(task_type, task_target_id, detail_json)

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = (
            postgresql_insert(TaskLog)
            .values(
                task_type=task_type,
                task_target_id=task_target_id,
                dedupe_key=dedupe_key,
                status=TaskStatus.PENDING,
                recovery_attempt=int(detail_payload.get("recovery_attempt") or 0),
                detail_json=detail_payload,
            )
            .on_conflict_do_nothing(
                index_elements=[TaskLog.dedupe_key],
                index_where=text(ACTIVE_DEDUPE_INDEX_PREDICATE),
            )
            .returning(TaskLog.id)
        )
        inserted_id = db.execute(stmt).scalar_one_or_none()
        db.flush()
        if inserted_id is None:
            existing = find_duplicate_active_task(db, task_type, task_target_id, dedupe_key)
            if existing is None:
                raise RuntimeError(f"Active task log not found for dedupe_key={dedupe_key}")
            return existing, False

        created = db.query(TaskLog).filter(TaskLog.id == inserted_id).one()
        return created, True

    existing = find_duplicate_active_task(db, task_type, task_target_id, dedupe_key)
    if existing is not None:
        return existing, False

    task_log = TaskLog(
        task_type=task_type,
        task_target_id=task_target_id,
        dedupe_key=dedupe_key,
        status=TaskStatus.PENDING,
        recovery_attempt=int(detail_payload.get("recovery_attempt") or 0),
        detail_json=detail_payload,
    )
    db.add(task_log)
    db.flush()
    db.refresh(task_log)
    return task_log, True


__all__ = ["create_pending_task_log"]
