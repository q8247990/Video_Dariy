"""Task maintenance: heartbeat, timeout recovery, log cleanup.

The heartbeat task runs every 60 seconds and is responsible for:
1. Auto-launching hot_build_task for each enabled video source
2. Checking full_build_task status
3. Recovering timed-out tasks
4. Cleaning up old task logs
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from math import ceil

from sqlalchemy.orm import Session

from src.core.celery_app import celery_app
from src.core.config import settings
from src.db.session import task_db_session
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    ScanMode,
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.services.task_dispatch_control import is_singleton_task_running

logger = logging.getLogger(__name__)

CLEANUP_DAYS = 7
HOT_BUILD_TIMEOUT_SECONDS = 3600
FULL_BUILD_TIMEOUT_SECONDS = 259200  # 3 days
ANALYSIS_BASE_TIMEOUT_SECONDS = 600


def _terminal_statuses() -> list[str]:
    return [
        TaskStatus.SUCCESS,
        TaskStatus.SKIPPED,
        TaskStatus.FAILED,
        TaskStatus.TIMEOUT,
        TaskStatus.CANCELLED,
    ]


def _dispatch_hot_builds(db: Session) -> list[dict]:
    """Launch hot_build_task for each enabled, non-paused source that has no active hot task."""
    sources = (
        db.query(VideoSource)
        .filter(VideoSource.enabled.is_(True), VideoSource.source_paused.is_(False))
        .all()
    )
    dispatched: list[dict] = []
    for source in sources:
        if is_singleton_task_running(db, TaskType.SESSION_BUILD, source.id, scan_mode=ScanMode.HOT):
            continue
        try:
            task = celery_app.send_task(
                "src.tasks.session_build.hot_build_task",
                kwargs={"source_id": source.id},
            )
            dispatched.append({"source_id": source.id, "task_id": str(task.id)})
        except Exception:
            logger.exception("Failed to dispatch hot build for source %s", source.id)
    return dispatched


def _task_timeout_seconds(db: Session, task_log: TaskLog) -> int:
    """Determine timeout for a task based on its type."""
    if task_log.task_type == TaskType.SESSION_BUILD:
        detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
        if detail.get("scan_mode") == ScanMode.FULL:
            return FULL_BUILD_TIMEOUT_SECONDS
        return HOT_BUILD_TIMEOUT_SECONDS

    if task_log.task_type == TaskType.SESSION_ANALYSIS:
        if task_log.task_target_id is None:
            return ANALYSIS_BASE_TIMEOUT_SECONDS
        session = db.query(VideoSession).filter(VideoSession.id == task_log.task_target_id).first()
        if not session:
            return ANALYSIS_BASE_TIMEOUT_SECONDS
        segment_seconds = max(1, int(settings.ANALYZER_SEGMENT_SECONDS or 600))
        total_duration = int(session.total_duration_seconds or 0)
        chunk_count = max(1, ceil(total_duration / segment_seconds))
        return ANALYSIS_BASE_TIMEOUT_SECONDS * chunk_count

    if task_log.task_type == TaskType.DAILY_SUMMARY_GENERATION:
        return 300

    return ANALYSIS_BASE_TIMEOUT_SECONDS


def _recover_timed_out_tasks(db: Session, now: datetime) -> int:
    """Find and mark timed-out running tasks."""
    running_tasks = db.query(TaskLog).filter(TaskLog.status == TaskStatus.RUNNING).all()
    timeout_count = 0

    for item in running_tasks:
        started_at = item.started_at or item.created_at
        timeout_seconds = _task_timeout_seconds(db, item)
        if started_at + timedelta(seconds=timeout_seconds) > now:
            continue

        if item.queue_task_id:
            try:
                celery_app.control.revoke(item.queue_task_id, terminate=True)
            except Exception:
                logger.exception("Failed to revoke timed-out task_id=%s", item.queue_task_id)

        item.status = TaskStatus.TIMEOUT
        item.message = f"Task timed out after {timeout_seconds} seconds"
        item.finished_at = now

        # Reset session status if analysis timed out
        if item.task_type == TaskType.SESSION_ANALYSIS and item.task_target_id is not None:
            session = db.query(VideoSession).filter(VideoSession.id == item.task_target_id).first()
            if session is not None and session.analysis_status == SessionAnalysisStatus.ANALYZING:
                session.analysis_status = SessionAnalysisStatus.SEALED

        timeout_count += 1

    return timeout_count


def _recover_orphan_pending_tasks(db: Session, now: datetime) -> int:
    """Mark stale pending tasks (>120s old with no worker) as timed out."""
    stale_before = now - timedelta(seconds=120)
    pending_tasks = (
        db.query(TaskLog)
        .filter(TaskLog.status == TaskStatus.PENDING, TaskLog.created_at <= stale_before)
        .order_by(TaskLog.created_at.asc())
        .all()
    )

    recovered = 0
    for item in pending_tasks:
        if item.queue_task_id:
            state = celery_app.AsyncResult(item.queue_task_id).state
            if state not in {"PENDING", "SUCCESS", "FAILURE", "REVOKED"}:
                continue

        item.status = TaskStatus.TIMEOUT
        item.finished_at = now
        item.message = "Pending task orphaned"

        # Reset session status if analysis orphaned
        if item.task_type == TaskType.SESSION_ANALYSIS and item.task_target_id is not None:
            session = db.query(VideoSession).filter(VideoSession.id == item.task_target_id).first()
            if session is not None and session.analysis_status == SessionAnalysisStatus.ANALYZING:
                session.analysis_status = SessionAnalysisStatus.SEALED

        recovered += 1

    return recovered


def _cleanup_old_task_logs(db: Session) -> int:
    """Delete task logs older than CLEANUP_DAYS."""
    threshold = datetime.now() - timedelta(days=CLEANUP_DAYS)
    deleted = (
        db.query(TaskLog)
        .filter(
            TaskLog.created_at < threshold,
            TaskLog.status.in_(_terminal_statuses()),
        )
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


@celery_app.task(bind=True)
def heartbeat(self) -> dict:
    """Main heartbeat: runs every 60s via Celery Beat."""
    with task_db_session() as db:
        now = datetime.now()
        try:
            # 1. Dispatch hot builds for all enabled sources
            dispatched_hot = _dispatch_hot_builds(db)

            # 2. Recover timed-out tasks
            timeout_count = _recover_timed_out_tasks(db, now)

            # 3. Recover orphan pending tasks
            pending_recovered = _recover_orphan_pending_tasks(db, now)

            # 4. Cleanup old logs (only run once per hour approximately)
            logs_deleted = 0
            if now.minute == 0:
                logs_deleted = _cleanup_old_task_logs(db)

            db.commit()
            return {
                "dispatched_hot": len(dispatched_hot),
                "timed_out": timeout_count,
                "pending_recovered": pending_recovered,
                "logs_deleted": logs_deleted,
            }
        except Exception:
            db.rollback()
            logger.exception("Heartbeat failed")
            raise
