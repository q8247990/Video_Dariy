"""Finalize policy — terminal status transitions for a TaskLog row.

Owns the in-place write that flips a row from RUNNING (or PENDING, in
the cancelled-by-user path) to a terminal status. The companion
helper :func:`finalize_cancelled_task_log` additionally sets
``cancel_requested=True`` so subsequent stages short-circuit on the
"user pressed stop" signal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from src.models.task_log import TaskLog
from src.services.dispatch.dedupe import ensure_dict_detail
from src.services.pipeline_constants import TaskStatus


def finalize_task_log(
    task_log: TaskLog,
    status: str,
    message: Optional[str] = None,
    detail_json: Optional[dict[str, Any]] = None,
) -> None:
    """Flip ``task_log`` into ``status``, stamp ``finished_at``, merge details.

    The transition matrix this helper enforces is the in-place
    counterpart to the centralised CAS UPDATE in
    :func:`src.services.pipeline_state.transition_task_log`. Use this
    helper for **direct** finalization (the worker already owns the
    row and the state-machine audit lives in the caller's session).
    Use :func:`transition_task_log` when the row's status must be
    moved from a non-terminal status with full audit-row machinery.

    Merges ``detail_json`` keyword into the existing
    :attr:`TaskLog.detail_json` payload; existing keys win only when
    the new value is missing (i.e. ``dict.update`` semantics).
    """
    task_log.status = status
    task_log.message = message
    task_log.finished_at = datetime.now(timezone.utc)
    if detail_json is not None:
        merged = ensure_dict_detail(task_log.detail_json)
        merged.update(detail_json)
        task_log.detail_json = merged


def finalize_cancelled_task_log(
    task_log: TaskLog,
    message: Optional[str] = None,
    detail_json: Optional[dict[str, Any]] = None,
) -> None:
    """Finalize ``task_log`` as CANCELLED with the ``cancel_requested`` flag set.

    The "user pressed stop" finalizer: sets ``cancel_requested=True``
    *and* ``status=CANCELLED`` so the audit log shows both the
    request and the resulting state in a single transition.
    """
    task_log.cancel_requested = True
    finalize_task_log(
        task_log,
        TaskStatus.CANCELLED,
        message or "Task cancelled by user",
        detail_json,
    )


__all__ = [
    "finalize_cancelled_task_log",
    "finalize_task_log",
]
