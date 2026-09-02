"""Lease policy — take, renew and verify worker leases on a TaskLog row.

The lease is the heart-beat the maintenance recovery policy uses to
distinguish a live worker from a crashed one:

* Each :func:`src.services.dispatch.worker_bind.bind_or_create_running_task_log`
  call stamps ``lease_expires_at = now + ANALYSIS_LEASE_SECONDS``.
* Every successful sub-chunk checkpoint (:mod:`src.services.analysis.checkpoint_writer`)
  calls :func:`renew_task_lease` to push ``lease_expires_at`` forward.
* The maintenance heartbeat (``recover_timed_out_tasks``) treats a
  TaskLog whose ``lease_expires_at`` has passed as crashed and
  re-dispatches it (or marks it terminal after the recovery budget
  is exhausted).

Renewal is a no-op when the row is missing or not RUNNING — the
worker that just committed a checkpoint is racing against the
heartbeat that may already have flipped the row to TIMEOUT.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.task_log import TaskLog
from src.services.pipeline_constants import TaskStatus


def renew_task_lease(db: Session, task_log_id: int, lease_owner: Optional[str] = None) -> None:
    """Push ``lease_expires_at`` forward by ``ANALYSIS_LEASE_SECONDS``.

    Called from the analyzer stage after every successful sub-chunk
    checkpoint write — the active sub-chunk work proves the worker
    is alive. No-op when:

    * the row is missing (recovery already finalized it), or
    * the row is no longer RUNNING (a concurrent recovery beat us
      to it and flipped it to TIMEOUT).
    """
    task_log = db.query(TaskLog).filter(TaskLog.id == task_log_id).first()
    if task_log is None or task_log.status != TaskStatus.RUNNING:
        return
    now = datetime.now(timezone.utc)
    task_log.lease_owner = lease_owner or task_log.lease_owner
    task_log.last_heartbeat_at = now
    task_log.lease_expires_at = now + timedelta(seconds=settings.ANALYSIS_LEASE_SECONDS)


__all__ = ["renew_task_lease"]
