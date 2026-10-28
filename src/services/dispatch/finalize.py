"""Finalize policy — terminal status transitions for a TaskLog row.

Owns the in-place write that flips a row from RUNNING (or PENDING, in
the cancelled-by-user path) to a terminal status. The companion
helper :func:`finalize_cancelled_task_log` additionally sets
``cancel_requested=True`` so subsequent stages short-circuit on the
"user pressed stop" signal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.core.pipeline_constants import TaskStatus
from src.models.task_log import TaskLog
from src.services.dispatch.dedupe import ensure_dict_detail
from src.services.pipeline_state import apply_task_status


def finalize_task_log(
    db: Session,
    task_log: TaskLog,
    status: str,
    message: str | None = None,
    detail_json: dict[str, Any] | None = None,
) -> None:
    """Flip ``task_log`` into ``status``, stamp ``finished_at``, merge details.

    The status write goes through
    :func:`src.services.pipeline_state.apply_task_status`, so the inline
    ``detail_json`` audit and the append-only ``pipeline_transition_log``
    row land in the caller's session together with the business UPDATE.

    Merges ``detail_json`` keyword into the existing
    :attr:`TaskLog.detail_json` payload; existing keys win only when
    the new value is missing (i.e. ``dict.update`` semantics).
    """
    apply_task_status(
        db,
        task_log,
        TaskStatus(status),
        reason=message or f"Finalized as {status}",
        source="finalize_task_log",
    )
    task_log.message = message
    task_log.finished_at = datetime.now(timezone.utc)
    if detail_json is not None:
        merged = ensure_dict_detail(task_log.detail_json)
        merged.update(detail_json)
        task_log.detail_json = merged


def finalize_cancelled_task_log(
    db: Session,
    task_log: TaskLog,
    message: str | None = None,
    detail_json: dict[str, Any] | None = None,
) -> None:
    """Finalize ``task_log`` as CANCELLED with the ``cancel_requested`` flag set.

    The "user pressed stop" finalizer: sets ``cancel_requested=True``
    *and* ``status=CANCELLED`` so the audit log shows both the
    request and the resulting state in a single transition.
    """
    task_log.cancel_requested = True
    finalize_task_log(
        db,
        task_log,
        TaskStatus.CANCELLED,
        message or "Task cancelled by user",
        detail_json,
    )


__all__ = [
    "finalize_cancelled_task_log",
    "finalize_task_log",
]
