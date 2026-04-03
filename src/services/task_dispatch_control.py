"""Task dispatch control: dedupe, singleton check, lifecycle management."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.services.pipeline_constants import TaskStatus


class TaskCancellationRequested(Exception):
    """Raised when a running task observes a user cancellation request."""


def get_task_log_for_update(db: Session, task_log_id: int) -> Optional[TaskLog]:
    return db.query(TaskLog).filter(TaskLog.id == task_log_id).first()


def is_task_cancel_requested(db: Session, task_log_id: int) -> bool:
    row = db.query(TaskLog.cancel_requested).filter(TaskLog.id == task_log_id).first()
    if row is None:
        return False
    return bool(row[0])


def ensure_task_not_cancelled(
    db: Session,
    task_log_id: int,
    *,
    default_message: str = "Task cancelled by user",
) -> None:
    if is_task_cancel_requested(db, task_log_id):
        raise TaskCancellationRequested(default_message)


def build_dedupe_key(
    task_type: str, task_target_id: Optional[int], detail_json: dict[str, Any]
) -> str:
    if task_type == "session_build":
        return "|".join([task_type, str(task_target_id), str(detail_json.get("scan_mode") or "")])
    if task_type == "session_analysis":
        return "|".join([task_type, str(task_target_id)])
    if task_type == "daily_summary_generation":
        return "|".join([task_type, str(detail_json.get("target_date") or "")])
    return "|".join([task_type, str(task_target_id), str(detail_json)])


def ensure_dict_detail(detail_json: Any) -> dict[str, Any]:
    if isinstance(detail_json, dict):
        return {str(key): value for key, value in detail_json.items()}
    return {}


def _ensure_dedupe_key(
    task_type: str,
    task_target_id: Optional[int],
    detail_json: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    detail_payload = ensure_dict_detail(detail_json)
    dedupe_key = str(detail_payload.get("dedupe_key") or "")
    if not dedupe_key:
        dedupe_key = build_dedupe_key(task_type, task_target_id, detail_payload)
        detail_payload["dedupe_key"] = dedupe_key
    return detail_payload, dedupe_key


def find_duplicate_active_task(
    db: Session,
    task_type: str,
    task_target_id: Optional[int],
    dedupe_key: str,
) -> Optional[TaskLog]:
    active_by_key = (
        db.query(TaskLog)
        .filter(
            TaskLog.dedupe_key == dedupe_key,
            TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .order_by(TaskLog.created_at.desc())
        .first()
    )
    if active_by_key is not None:
        return active_by_key

    query = db.query(TaskLog).filter(
        TaskLog.task_type == task_type,
        TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        TaskLog.dedupe_key.is_(None),
    )
    if task_target_id is None:
        query = query.filter(TaskLog.task_target_id.is_(None))
    else:
        query = query.filter(TaskLog.task_target_id == task_target_id)

    candidates = query.order_by(TaskLog.created_at.desc()).all()
    for row in candidates:
        detail = ensure_dict_detail(row.detail_json)
        if str(detail.get("dedupe_key") or "") == dedupe_key:
            return row
    return None


def is_singleton_task_running(
    db: Session,
    task_type: str,
    task_target_id: Optional[int],
    scan_mode: Optional[str] = None,
) -> bool:
    """Check if a singleton task (session_build hot/full) is already active."""
    query = db.query(TaskLog.id).filter(
        TaskLog.task_type == task_type,
        TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
    )
    if task_target_id is not None:
        query = query.filter(TaskLog.task_target_id == task_target_id)

    if scan_mode is not None:
        candidates = query.all()
        for row in candidates:
            task_log = db.query(TaskLog).filter(TaskLog.id == row[0]).first()
            if task_log:
                detail = ensure_dict_detail(task_log.detail_json)
                if detail.get("scan_mode") == scan_mode:
                    return True
        return False

    return query.first() is not None


def create_pending_task_log(
    db: Session,
    task_type: str,
    task_target_id: Optional[int],
    detail_json: dict[str, Any],
) -> tuple[TaskLog, bool]:
    detail_payload, dedupe_key = _ensure_dedupe_key(task_type, task_target_id, detail_json)

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = (
            postgresql_insert(TaskLog)
            .values(
                task_type=task_type,
                task_target_id=task_target_id,
                dedupe_key=dedupe_key,
                status=TaskStatus.PENDING,
                detail_json=detail_payload,
            )
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
        detail_json=detail_payload,
    )
    db.add(task_log)
    db.flush()
    db.refresh(task_log)
    return task_log, True


def bind_or_create_running_task_log(  # noqa: C901
    db: Session,
    queue_task_id: Optional[str],
    task_type: str,
    task_target_id: Optional[int],
    detail_json: Optional[dict[str, Any]] = None,
) -> TaskLog:
    detail_payload = ensure_dict_detail(detail_json)
    detail_payload, dedupe_key = _ensure_dedupe_key(task_type, task_target_id, detail_payload)
    now = datetime.now()

    def _apply_running_state(target: TaskLog) -> TaskLog:
        merged = ensure_dict_detail(target.detail_json)
        merged.update(detail_payload)
        target.task_target_id = task_target_id
        target.dedupe_key = dedupe_key
        target.queue_task_id = queue_task_id or target.queue_task_id
        target.status = TaskStatus.RUNNING
        target.started_at = target.started_at or now
        target.message = None
        target.detail_json = merged
        return target

    def _mark_superseded_pending(bind_id: int) -> None:
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

    if queue_task_id:
        existing = (
            db.query(TaskLog)
            .filter(TaskLog.queue_task_id == queue_task_id, TaskLog.task_type == task_type)
            .order_by(TaskLog.id.desc())
            .first()
        )
        if existing:
            bound = _apply_running_state(existing)
            _mark_superseded_pending(bound.id)
            return bound

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
        bound = _apply_running_state(item)
        _mark_superseded_pending(bound.id)
        return bound

    task_log = TaskLog(
        task_type=task_type,
        task_target_id=task_target_id,
        dedupe_key=dedupe_key,
        queue_task_id=queue_task_id,
        status=TaskStatus.RUNNING,
        started_at=now,
        detail_json=detail_payload,
    )
    db.add(task_log)
    return task_log


def finalize_task_log(
    task_log: TaskLog,
    status: str,
    message: Optional[str] = None,
    detail_json: Optional[dict[str, Any]] = None,
) -> None:
    task_log.status = status
    task_log.message = message
    task_log.finished_at = datetime.now()
    if detail_json is not None:
        merged = ensure_dict_detail(task_log.detail_json)
        merged.update(detail_json)
        task_log.detail_json = merged


def finalize_cancelled_task_log(
    task_log: TaskLog,
    message: Optional[str] = None,
    detail_json: Optional[dict[str, Any]] = None,
) -> None:
    task_log.cancel_requested = True
    finalize_task_log(
        task_log,
        TaskStatus.CANCELLED,
        message or "Task cancelled by user",
        detail_json,
    )
