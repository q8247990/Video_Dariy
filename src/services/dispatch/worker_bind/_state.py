"""Worker-bind helpers: state mutation + superseded-pending cancellation.

Internal helpers used by the
:func:`src.services.dispatch.worker_bind.bind_or_create_running_task_log`
orchestrator. Split out so the orchestrator module stays free of
state-mutation concerns and each helper is independently
unit-testable against a minimal TaskLog table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.services.dispatch.dedupe import ensure_dict_detail
from src.services.pipeline_constants import TaskStatus


def apply_running_state(
    target: TaskLog,
    *,
    detail_payload: dict[str, Any],
    task_type: str,
    task_target_id: Optional[int],
    dedupe_key: str,
    queue_task_id: Optional[str],
    now: datetime,
    lease_seconds: int,
) -> TaskLog:
    """Mutate ``target`` into the RUNNING state with a fresh lease.

    Preserves ``started_at`` when the row already had one (rebind of a
    still-running row from a redelivered broker message); always
    refreshes ``last_heartbeat_at`` and ``lease_expires_at``.
    """
    from datetime import timedelta

    merged = ensure_dict_detail(target.detail_json)
    merged.update(detail_payload)
    target.task_target_id = task_target_id
    target.dedupe_key = dedupe_key
    target.queue_task_id = queue_task_id or target.queue_task_id
    target.status = TaskStatus.RUNNING
    target.started_at = target.started_at or now
    target.lease_owner = queue_task_id or target.lease_owner
    target.last_heartbeat_at = now
    target.lease_expires_at = now + timedelta(seconds=lease_seconds)
    target.message = None
    target.detail_json = merged
    return target


def mark_superseded_pending(
    db: Session,
    *,
    bind_id: int,
    task_type: str,
    task_target_id: Optional[int],
    dedupe_key: str,
    now: datetime,
) -> None:
    """Flip every other PENDING TaskLog that shares the dedupe-key to CANCELLED.

    Only pending candidates are touched (the active ``bind_id`` is always
    RUNNING by the time we get here). The status flip is an in-place
    attribute set; the caller owns the commit.
    """
    candidates = (
        db.query(TaskLog)
        .filter(
            TaskLog.id != bind_id,
            TaskLog.task_type == task_type,
            TaskLog.status == TaskStatus.PENDING,
        )
        .order_by(TaskLog.id.desc())
        .all()
    )
    for item in candidates:
        if task_target_id is None and item.task_target_id is not None:
            continue
        if task_target_id is not None and item.task_target_id != task_target_id:
            continue
        item_detail = ensure_dict_detail(item.detail_json)
        if str(item_detail.get("dedupe_key") or "") != dedupe_key:
            continue
        item.status = TaskStatus.CANCELLED
        item.finished_at = now
        item.message = "Superseded by running task binding"


__all__ = [
    "apply_running_state",
    "mark_superseded_pending",
]
