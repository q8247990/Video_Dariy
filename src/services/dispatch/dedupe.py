"""Dedupe / run-claim policy.

Owns the dedupe-key construction and the "find the existing active
TaskLog" lookup that gates every dispatch call. The helpers here are
the seam between the dispatcher (which asks "is there an active row
for this work?") and the persistence layer (TaskLog rows).

Split out from the pre-Wave-5 ``task_dispatch_control.py`` so the
policy can be unit-tested against an in-memory TaskLog table without
standing up the rest of the dispatch-control pipeline. The functions
are pure of side effects except for the SQLAlchemy session argument
they take.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.services.pipeline_constants import TaskStatus, TaskType


def ensure_dict_detail(detail_json: Any) -> dict[str, Any]:
    """Coerce a possibly-string / None ``detail_json`` into a plain dict.

    Tasks store arbitrary per-call metadata in
    :attr:`TaskLog.detail_json` (scan mode, priority, recovery attempt,
    …). The column is typed ``JSON`` but the model accepts anything;
    the dispatcher and the worker-bind path both treat it as a dict.
    This helper is the single source of truth for the coercion.
    """
    if isinstance(detail_json, dict):
        return {str(key): value for key, value in detail_json.items()}
    return {}


def build_dedupe_key(
    task_type: str,
    task_target_id: Optional[int],
    detail_json: dict[str, Any],
) -> str:
    """Compute the dedupe-key that pins one logical work unit to one active row.

    The key rules are preserved verbatim from the pre-Wave-5 monolith:

    * ``session_build`` always dedupes by source (the source scan is the
      logical unit — hot and full are mutually exclusive regardless of
      scan mode).
    * ``session_analysis`` dedupes by ``(task_type, task_target_id)``.
    * ``daily_summary_generation`` dedupes by ``(task_type, target_date)``.
    * Everything else falls back to
      ``f"{task_type}|{task_target_id}|{detail_json}"``.
    """
    if task_type == TaskType.SESSION_BUILD:
        return f"source_scan:{task_target_id}"
    if task_type == TaskType.SESSION_ANALYSIS:
        return "|".join([task_type, str(task_target_id)])
    if task_type == TaskType.DAILY_SUMMARY_GENERATION:
        return "|".join([task_type, str(detail_json.get("target_date") or "")])
    return "|".join([task_type, str(task_target_id), str(detail_json)])


def _ensure_dedupe_key(
    task_type: str,
    task_target_id: Optional[int],
    detail_json: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Resolve the dedupe-key, mutating ``detail_json`` to carry it when computed.

    Used by :func:`src.services.dispatch.claim.create_pending_task_log`
    and :func:`src.services.dispatch.worker_bind.bind_or_create_running_task_log`
    so a freshly-constructed TaskLog row always has its dedupe-key
    embedded in the JSON payload (the dispatcher relies on that for the
    HOT↔FULL precedence rule).
    """
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
    """Return the active (PENDING / RUNNING) TaskLog for the dedupe-key, if any.

    Uses the task-type-specific lookup shape:

    * ``session_build``: ignores the dedupe-key (HOT and FULL share the
      same source-level lock); looks up by
      ``(task_type, task_target_id, status IN [PENDING, RUNNING])``
      ordered by ``created_at desc``.
    * All other task types: first scans for an active row whose
      ``dedupe_key`` matches; falls back to a load-bearing legacy path
      that scans ``dedupe_key IS NULL`` rows whose ``detail_json``
      carries the same dedupe_key (the pre-partial-unique-index path
      we still respect because SQLite unit tests do not create the PG
      partial unique index).
    """
    if task_type == TaskType.SESSION_BUILD:
        return (
            db.query(TaskLog)
            .filter(
                TaskLog.task_type == task_type,
                TaskLog.task_target_id == task_target_id,
                TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
            )
            .order_by(TaskLog.created_at.desc())
            .first()
        )

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
    """Check if a singleton task (session_build hot/full) is already active.

    The ``scan_mode`` filter is the user-facing precedence check:
    a HOT build is refused while a FULL is active and vice versa. The
    fallback (no ``scan_mode``) returns ``True`` when **any** active row
    exists for ``(task_type, task_target_id)``.
    """
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


__all__ = [
    "build_dedupe_key",
    "ensure_dict_detail",
    "find_duplicate_active_task",
    "is_singleton_task_running",
]
