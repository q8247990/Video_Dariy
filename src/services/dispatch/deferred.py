"""Deferred / superseded scan policy — HOT ↔ FULL precedence.

The HOT/FULL precedence rule is the only consumer of the two
counters in this module:

* HOT over an active FULL → record a deferred (skipped) TaskLog.
  The active FULL already owns the publish intent, so no second
  outbox row is created.
* FULL over an active HOT → cancel the HOT (mark it CANCELLED with
  the ``superseded=True`` flag), then dispatch the FULL via the
  caller's normal claim path.

The functions are intentionally small — they only mutate the
``TaskLog`` state — and rely on the caller (``CeleryTaskDispatcher``)
to issue the matching outbox event for the FULL case.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.services.dispatch.dedupe import build_dedupe_key, ensure_dict_detail
from src.services.pipeline_constants import TaskStatus, TaskType


def record_deferred_hot_scan(
    db: Session,
    source_id: int,
    active_task_log: TaskLog,
) -> TaskLog:
    """Insert a SKIPPED TaskLog row for the deferred HOT scan.

    Carries the reason (``full_scan_in_progress``) and the active
    FULL's ``task_log_id`` in ``detail_json`` so operators can trace
    the precedence decision back to its driver.
    """
    detail = ensure_dict_detail(active_task_log.detail_json)
    deferred_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source_id,
        dedupe_key=build_dedupe_key(TaskType.SESSION_BUILD, source_id, detail),
        status=TaskStatus.SKIPPED,
        message=f"Deferred hot scan: full scan in progress for source {source_id}",
        detail_json={
            "scan_mode": "hot",
            "source_id": source_id,
            "deferred": True,
            "reason": "full_scan_in_progress",
            "active_task_log_id": active_task_log.id,
        },
    )
    db.add(deferred_log)
    db.flush()
    return deferred_log


def supersede_active_hot_scan(
    db: Session,
    source_id: int,
    active_task_log: TaskLog,
) -> None:
    """Cancel ``active_task_log`` (the in-flight HOT) so the FULL can claim the source.

    Flips the row to ``CANCELLED`` with the ``superseded=True`` and
    ``superseded_by=full_scan_request`` audit flags; the FULL
    dispatcher then issues a fresh ``create_pending_task_log`` /
    ``enqueue_command`` pair against the same source.

    Kept as a dedicated policy (rather than a flag on
    :func:`src.services.dispatch.finalize.finalize_task_log`) because
    the precedence rule is product-level: a HOT-vs-FULL collision
    is its own decision tree, not a generic "cancel a task" path.
    """
    active_task_log.status = TaskStatus.CANCELLED
    active_task_log.finished_at = datetime.now(timezone.utc)
    active_task_log.message = f"Superseded by full scan request for source {source_id}"
    active_task_log.detail_json = {
        **ensure_dict_detail(active_task_log.detail_json),
        "superseded": True,
        "superseded_by": "full_scan_request",
    }
    db.flush()


__all__ = [
    "record_deferred_hot_scan",
    "supersede_active_hot_scan",
]
