"""Unleased-pending recovery — time out PENDING tasks never picked up.

The :mod:`.unleased_recovery` policy handles the "broker message was
lost / worker never picked up the task" case. The pre-Wave-5 monolith
folded this into the orphan-pending loop; Wave 5 splits it into a
self-contained policy module so the heartbeat aggregator can call
it independently and the unit tests can pin its behaviour against a
minimal TaskLog table.

The two policies:

* :func:`recover_unleased_pending_tasks` — sweep PENDING rows whose
  ``lease_expires_at IS NULL`` (no worker ever leased them) and whose
  ``created_at`` is older than
  ``PENDING_UNLEASSED_TIMEOUT_SECONDS``; the analyzer task type is
  excluded (its dedicated queue may legitimately hold pending rows
  for long stretches on the single-concurrency vision worker).
* :func:`recover_orphan_pending_tasks` — sweep PENDING rows whose
  lease *did* expire (the worker held a lease, then crashed); the
  analyzer lease-expiry path delegates to
  :func:`~src.services.maintenance.analysis_recovery.resume_lost_analysis`.

Each helper returns the deterministic ``int`` counter the heartbeat
aggregator reports as ``pending_recovered``.

Concurrency safety
==================

Both sweeps issue the PENDING-row SELECT with ``with_for_update()``
(``SELECT ... FOR UPDATE`` on PostgreSQL, a no-op on the SQLite test
dialect) so concurrent heartbeat/worker recoveries claim each row
exclusively. Without the lock, two sweeps can read the same PENDING
rows, each flip them to TIMEOUT, and both inflate their
``pending_recovered`` counter — the double-claim the Wave-5
``tests/integration/test_dispatch_maintenance_postgres.py::test_concurrent_orphan_recovery_creates_one_recovery``
spec pins down.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.core.celery_app import celery_app
from src.core.config import settings
from src.models.task_log import TaskLog
from src.services.maintenance.analysis_recovery import resume_lost_analysis
from src.services.pipeline_constants import TaskStatus, TaskType

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def recover_unleased_pending_tasks(db: Session, now: datetime) -> int:
    """Time out PENDING tasks that were never leased by any worker.

    Covers broker message loss / worker outage. Without this, such a
    task stays pending forever and, for session builds, keeps
    deferring hot scans. Analysis tasks are excluded: they may
    legitimately queue for long stretches on the single-concurrency
    vision worker.

    The broker-side revoke (``celery_app.control.revoke``) is issued
    when ``queue_task_id`` is set so a late broker delivery cannot
    re-bind and resurrect the timed-out row via
    :func:`src.services.dispatch.worker_bind.bind_or_create_running_task_log`'s
    queue_task_id path.

    Returns the deterministic count of rows that flipped to TIMEOUT
    in this call (the heartbeat aggregator reports it as
    ``pending_unleased_recovered``).
    """
    unleased_before = now - timedelta(seconds=settings.PENDING_UNLEASSED_TIMEOUT_SECONDS)
    pending_tasks = (
        db.query(TaskLog)
        .filter(
            TaskLog.status == TaskStatus.PENDING,
            TaskLog.lease_expires_at.is_(None),
            TaskLog.created_at <= unleased_before,
        )
        .order_by(TaskLog.created_at.asc())
        .with_for_update()
        .all()
    )

    recovered = 0
    for item in pending_tasks:
        if item.cancel_requested or item.task_type == TaskType.SESSION_ANALYSIS:
            continue
        if item.queue_task_id:
            try:
                celery_app.control.revoke(item.queue_task_id, terminate=True)
            except Exception:
                logger.exception("Failed to revoke unleased task_id=%s", item.queue_task_id)
        item.status = TaskStatus.TIMEOUT
        item.finished_at = now
        item.message = "Pending task was never picked up by a worker; timed out"
        recovered += 1

    return recovered


def recover_orphan_pending_tasks(db: Session, now: datetime) -> int:
    """Time out PENDING tasks whose worker lease expired.

    Sweeps the two disjoint PENDING populations:

    * never-leased (``lease_expires_at IS NULL``) — delegates to
      :func:`recover_unleased_pending_tasks`.
    * lease-expired (``lease_expires_at <= now``) — the analyzer
      branch delegates to
      :func:`~src.services.maintenance.analysis_recovery.resume_lost_analysis`,
      every other task type is finalised as ``TIMEOUT``.

    Returns the deterministic count of rows that flipped to a
    terminal status in this call (the heartbeat aggregator reports
    it as ``pending_recovered``).
    """
    recovered = recover_unleased_pending_tasks(db, now)
    stale_before = now - timedelta(seconds=settings.ANALYSIS_PENDING_GRACE_SECONDS)
    pending_tasks = (
        db.query(TaskLog)
        .filter(TaskLog.status == TaskStatus.PENDING, TaskLog.created_at <= stale_before)
        .order_by(TaskLog.created_at.asc())
        .with_for_update()
        .all()
    )

    for item in pending_tasks:
        if (
            item.cancel_requested
            or item.lease_expires_at is None
            or _as_utc(item.lease_expires_at) > now
        ):
            continue
        if item.task_type == TaskType.SESSION_ANALYSIS:
            resume_lost_analysis(db, item, now)
        else:
            item.status = TaskStatus.TIMEOUT
            item.finished_at = now
            item.message = "Pending task orphaned after expired worker lease"

        recovered += 1

    return recovered


__all__ = [
    "recover_orphan_pending_tasks",
    "recover_unleased_pending_tasks",
]
