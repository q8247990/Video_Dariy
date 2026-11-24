"""Retention cleanup — delete terminal TaskLog rows older than the threshold.

The :func:`cleanup_old_task_logs` policy runs **once per hour** from
the heartbeat aggregator (when ``now.minute == 0``). Three side tables
carry a ``task_log_id`` FK with ``ON DELETE SET NULL`` and therefore
outlive the TaskLog window: ``pipeline_transition_log``,
``daily_summary_attempt`` and ``outbox_event`` — the operational
audit / delivery ledgers are independent of the per-task lifecycle
history. Because the ``outbox_event`` FK no longer blocks the bulk
``DELETE`` (it used to be ``RESTRICT``, which made every hourly
heartbeat raise ``ForeignKeyViolation`` and permanently kill both the
retention and the missing-file sweep), the policy additionally prunes
terminal (``published`` / ``failed``) outbox rows past the threshold
so that ledger cannot grow forever. It deletes those rows **before**
the TaskLog sweep.

The threshold is :data:`src.services.maintenance.constants.CLEANUP_DAYS`
(``7``); ``cleanup_old_task_logs`` accepts ``now`` as an explicit
parameter so tests can pin the deletion window deterministically.

The bulk ``DELETE`` uses ``synchronize_session=False`` so SQLAlchemy
does not try to fetch every matched row into the session's identity
map (the legacy 392-row catalog would otherwise blow up the unit
test's per-call memory budget).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from src.models.outbox import OutboxEvent
from src.models.task_log import TaskLog
from src.services.dispatch.constants import TERMINAL_TASK_STATUSES
from src.services.maintenance.constants import cleanup_threshold

logger = logging.getLogger(__name__)

#: Outbox lifecycle states that never change again and may be pruned.
TERMINAL_OUTBOX_STATUSES: tuple[str, ...] = ("published", "failed")


def cleanup_old_task_logs(db: Session, now: datetime) -> int:
    """Delete terminal TaskLog rows whose ``created_at`` is older than 7 days.

    Prunes terminal ``outbox_event`` rows past the same threshold first
    so the TaskLog bulk DELETE cannot be blocked by a RESTRICT-style
    reference, then deletes the TaskLog rows. Returns the deterministic
    count of deleted ``task_log`` rows (the heartbeat aggregator
    reports it as ``logs_deleted``).
    """
    threshold = cleanup_threshold(now)
    outbox_deleted = (
        db.query(OutboxEvent)
        .filter(
            OutboxEvent.created_at < threshold,
            OutboxEvent.status.in_(TERMINAL_OUTBOX_STATUSES),
        )
        .delete(synchronize_session=False)
    )
    outbox_deleted = int(outbox_deleted or 0)
    if outbox_deleted:
        logger.info(
            "Retention deleted %s terminal outbox_event rows older than %s",
            outbox_deleted,
            threshold,
        )
    deleted = (
        db.query(TaskLog)
        .filter(
            TaskLog.created_at < threshold,
            TaskLog.status.in_(TERMINAL_TASK_STATUSES),
        )
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


__all__ = ["TERMINAL_OUTBOX_STATUSES", "cleanup_old_task_logs"]
