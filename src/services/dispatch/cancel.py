"""Cancel policy — user-cancellation observation helpers.

The Celery tasks observe the ``cancel_requested`` flag throughout
their lifetime (claim → chunk loop → finalize). The helpers in this
module are the single source of truth for the read paths; the write
side lives in :func:`src.services.dispatch.finalize.finalize_cancelled_task_log`
and the API endpoint in :mod:`src.api.v1.endpoints.tasks`.

Raising :class:`TaskCancellationRequested` is the cooperative-shutdown
mechanism: each consumer task checks the flag at every chunk
boundary and propagates the exception through its top-level
exception handler, which then calls
:func:`finalize_cancelled_task_log` to mark the row terminal.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from src.models.task_log import TaskLog


class TaskCancellationRequested(Exception):
    """Raised when a running task observes a user cancellation request.

    Workers catch this exception at their top-level exception handler
    and finalise the :class:`TaskLog` as ``CANCELLED`` with the
    ``cancel_requested=True`` audit flag set.
    """


def get_task_log_for_update(db: Session, task_log_id: int) -> Optional[TaskLog]:
    """Re-fetch a TaskLog row inside the cancellation / failure paths.

    Returns ``None`` when the row no longer exists (e.g. the
    7-day retention cleanup ran between the worker's read and the
    failure handler).
    """
    return db.query(TaskLog).filter(TaskLog.id == task_log_id).first()


def is_task_cancel_requested(db: Session, task_log_id: int) -> bool:
    """Return ``True`` when the user has requested cancellation of ``task_log_id``."""
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
    """Raise :class:`TaskCancellationRequested` when the user asked for cancellation.

    Workers call this helper at every chunk / sub-chunk boundary so
    a cancel request that arrives mid-run still propagates
    deterministically. ``default_message`` is operator-facing copy
    surfaced to the finalised TaskLog and the structured log.
    """
    if is_task_cancel_requested(db, task_log_id):
        raise TaskCancellationRequested(default_message)


__all__ = [
    "TaskCancellationRequested",
    "ensure_task_not_cancelled",
    "get_task_log_for_update",
    "is_task_cancel_requested",
]
