"""Retention cleanup — delete terminal TaskLog rows older than the threshold.

The :func:`cleanup_old_task_logs` policy runs **once per hour** from
the heartbeat aggregator (when ``now.minute == 0``). The
``pipeline_transition_log`` / ``daily_summary_attempt`` audit rows
outlive the TaskLog window because their FKs are ``ON DELETE SET
NULL`` rather than ``CASCADE`` / ``RESTRICT`` — the operational
audit trail is independent of the per-task lifecycle history.

The threshold is :data:`src.services.maintenance.constants.CLEANUP_DAYS`
(``7``); ``cleanup_old_task_logs`` accepts ``now`` as an explicit
parameter so tests can pin the deletion window deterministically.

The bulk ``DELETE`` uses ``synchronize_session=False`` so SQLAlchemy
does not try to fetch every matched row into the session's identity
map (the legacy 392-row catalog would otherwise blow up the unit
test's per-call memory budget).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.services.dispatch.constants import TERMINAL_TASK_STATUSES
from src.services.maintenance.constants import cleanup_threshold


def cleanup_old_task_logs(db: Session, now: datetime) -> int:
    """Delete terminal TaskLog rows whose ``created_at`` is older than 7 days.

    Returns the deterministic count of deleted rows (the heartbeat
    aggregator reports it as ``logs_deleted``).
    """
    threshold = cleanup_threshold(now)
    deleted = (
        db.query(TaskLog)
        .filter(
            TaskLog.created_at < threshold,
            TaskLog.status.in_(TERMINAL_TASK_STATUSES),
        )
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


__all__ = ["cleanup_old_task_logs"]
