"""Running-lease recovery — recover RUNNING tasks whose lease expired.

The :func:`recover_timed_out_tasks` policy is the "crashed worker"
detector. It scans the ``task_log`` table for RUNNING rows whose
``lease_expires_at`` has passed and whose per-task-type timeout
window has elapsed, then either:

* re-queues the analyzer lease-expiry case via the analysis
  recovery helper (:mod:`.analysis_recovery`), or
* marks the row TIMEOUT with an operator-facing message.

Pre-Wave-5 this loop issued a per-row SELECT for the
``SessionAnalysisCheckpoint`` count and the
``SessionAnalysisCheckpoint.attempt_count`` max — the classic N+1.
This module replaces those with **two batched aggregate queries**
before the loop so the loop body only mutates rows. The C901 budget
is honoured by splitting the per-task-type timeout computation into
:func:`_task_timeout_seconds_for` (pure data, no DB) plus a single
helper that loads the analysis checkpoint aggregates in one go.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.celery_app import celery_app
from src.core.config import settings
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.services.maintenance.analysis_recovery import resume_lost_analysis
from src.services.maintenance.constants import (
    ANALYSIS_BASE_TIMEOUT_SECONDS,
    ANALYSIS_RETRY_GRACE_SECONDS,
    DAILY_SUMMARY_TIMEOUT_SECONDS,
    FULL_BUILD_TIMEOUT_SECONDS,
    HOT_BUILD_TIMEOUT_SECONDS,
)
from src.services.pipeline_constants import TaskStatus, TaskType

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _load_analysis_aggregates(db: Session, session_ids: set[int]) -> dict[int, dict[str, int]]:
    """Return ``{session_id: {checkpoint_count, retry_count}}`` in two batched queries.

    Replaces the per-row ``SELECT count(*)`` / ``SELECT max(attempt_count)``
    pair the pre-Wave-5 monolith issued inside its loop. Each
    :class:`TaskLog` whose ``task_type == SESSION_ANALYSIS`` now
    does an O(1) dict lookup against this snapshot.
    """
    if not session_ids:
        return {}
    checkpoint_count_by_session: dict[int, int] = {
        row[0]: row[1]
        for row in db.query(
            SessionAnalysisCheckpoint.session_id, func.count(SessionAnalysisCheckpoint.id)
        )
        .filter(
            SessionAnalysisCheckpoint.session_id.in_(session_ids),
            SessionAnalysisCheckpoint.state == "success",
        )
        .group_by(SessionAnalysisCheckpoint.session_id)
        .all()
    }
    retry_count_by_session: dict[int, int] = {
        row[0]: row[1]
        for row in db.query(
            SessionAnalysisCheckpoint.session_id,
            func.max(SessionAnalysisCheckpoint.attempt_count),
        )
        .filter(SessionAnalysisCheckpoint.session_id.in_(session_ids))
        .group_by(SessionAnalysisCheckpoint.session_id)
        .all()
    }
    aggregates: dict[int, dict[str, int]] = {}
    for session_id in session_ids:
        aggregates[session_id] = {
            "checkpoint_count": checkpoint_count_by_session.get(session_id, 0),
            "retry_count": retry_count_by_session.get(session_id, 0),
        }
    return aggregates


def _load_last_progress(db: Session) -> dict[int, datetime]:
    """Return ``{session_id: most_recent_success_completed_at}`` (single batched query)."""
    rows = (
        db.query(
            SessionAnalysisCheckpoint.session_id,
            func.max(SessionAnalysisCheckpoint.completed_at),
        )
        .filter(
            SessionAnalysisCheckpoint.state == "success",
            SessionAnalysisCheckpoint.completed_at.is_not(None),
        )
        .group_by(SessionAnalysisCheckpoint.session_id)
        .all()
    )
    return {row[0]: row[1] for row in rows if row[1] is not None}


def _task_timeout_seconds_for(
    task_log: TaskLog,
    *,
    analysis_aggregates: dict[int, dict[str, int]],
) -> int:
    """Pick the per-task-type timeout window. Pure data, no DB.

    ``ANALYSIS_*`` rows look up ``analysis_aggregates`` (the batched
    dict loaded by :func:`_load_analysis_aggregates`) so the loop
    stays free of per-row SELECTs.
    """
    if task_log.task_type == TaskType.SESSION_BUILD:
        detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
        if detail.get("scan_mode") == "full":
            return FULL_BUILD_TIMEOUT_SECONDS
        return HOT_BUILD_TIMEOUT_SECONDS

    if task_log.task_type == TaskType.SESSION_ANALYSIS:
        if task_log.task_target_id is None:
            return settings.ANALYSIS_PROGRESS_GRACE_SECONDS
        aggregate = analysis_aggregates.get(task_log.task_target_id, {})
        retry_count = int(aggregate.get("retry_count") or 0)
        checkpoint_count = int(aggregate.get("checkpoint_count") or 0)
        retry_grace = retry_count * ANALYSIS_RETRY_GRACE_SECONDS
        progress_grace = settings.ANALYSIS_PROGRESS_GRACE_SECONDS + retry_grace
        if checkpoint_count:
            return max(settings.ANALYSIS_PROGRESS_GRACE_SECONDS, progress_grace)
        return max(ANALYSIS_BASE_TIMEOUT_SECONDS, progress_grace)

    if task_log.task_type == TaskType.DAILY_SUMMARY_GENERATION:
        return DAILY_SUMMARY_TIMEOUT_SECONDS

    return ANALYSIS_BASE_TIMEOUT_SECONDS


def _is_lease_expired(item: TaskLog, now: datetime) -> bool:
    return item.lease_expires_at is not None and _as_utc(item.lease_expires_at) <= now


def _handle_running_item(
    db: Session,
    item: TaskLog,
    *,
    now: datetime,
    analysis_aggregates: dict[int, dict[str, int]],
    last_progress_by_session: dict[int, datetime],
) -> bool:
    """One iteration of the lease-expiry loop. Returns ``True`` when the row was finalized."""
    if item.cancel_requested:
        return False
    started_at = _as_utc(item.started_at or item.created_at)
    timeout_seconds = _task_timeout_seconds_for(item, analysis_aggregates=analysis_aggregates)
    last_progress: Optional[datetime] = None
    if item.task_type == TaskType.SESSION_ANALYSIS and item.task_target_id is not None:
        last_progress = last_progress_by_session.get(item.task_target_id)
        if last_progress is not None:
            last_progress = _as_utc(last_progress)
    timeout_anchor = _as_utc(last_progress or item.last_heartbeat_at or started_at)
    lease_expired = _is_lease_expired(item, now)
    if timeout_anchor + timedelta(seconds=timeout_seconds) > now or not lease_expired:
        return False

    if item.queue_task_id:
        try:
            celery_app.control.revoke(item.queue_task_id, terminate=True)
        except Exception:
            logger.exception("Failed to revoke timed-out task_id=%s", item.queue_task_id)

    if item.task_type == TaskType.SESSION_ANALYSIS:
        resume_lost_analysis(db, item, now)
    else:
        item.status = TaskStatus.TIMEOUT
        item.message = f"Task timed out after {timeout_seconds} seconds"
        item.finished_at = now

    return True


def recover_timed_out_tasks(db: Session, now: datetime) -> int:
    """Recover RUNNING tasks whose lease expired.

    Single batched pass: one SELECT for the RUNNING rows, two
    batched aggregate queries for the analysis checkpoint counts,
    one batched SELECT for the latest progress timestamp. The loop
    body only mutates rows.

    Returns the number of rows that flipped to a terminal status in
    this call (the heartbeat aggregator reports it as the
    ``leases_recovered`` counter).
    """
    running_tasks = db.query(TaskLog).filter(TaskLog.status == TaskStatus.RUNNING).all()
    if not running_tasks:
        return 0
    analysis_session_ids = {
        item.task_target_id
        for item in running_tasks
        if item.task_type == TaskType.SESSION_ANALYSIS and item.task_target_id is not None
    }
    analysis_aggregates = _load_analysis_aggregates(db, analysis_session_ids)
    last_progress_by_session = _load_last_progress(db)

    timeout_count = 0
    for item in running_tasks:
        if _handle_running_item(
            db,
            item,
            now=now,
            analysis_aggregates=analysis_aggregates,
            last_progress_by_session=last_progress_by_session,
        ):
            timeout_count += 1
    return timeout_count


__all__ = ["recover_timed_out_tasks"]
