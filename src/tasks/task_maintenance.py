"""Task maintenance: heartbeat, timeout recovery, log cleanup.

The heartbeat task runs every 60 seconds and is responsible for:
1. Auto-launching hot_build_task for each enabled video source
2. Checking full_build_task status
3. Recovering timed-out tasks
4. Cleaning up old task logs
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.application.pipeline.commands import AnalyzeSessionCommand, SessionBuildCommand
from src.core.celery_app import celery_app
from src.core.config import settings
from src.db.session import task_db_session
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    ScanMode,
    SessionAnalysisStatus,
    SourceType,
    TaskStatus,
    TaskType,
)
from src.services.pipeline_state import transition_session, transition_task_log
from src.services.session_video import mark_missing_source_video_files
from src.tasks._container import get_container

logger = logging.getLogger(__name__)

CLEANUP_DAYS = 7
HOT_BUILD_TIMEOUT_SECONDS = 3600
FULL_BUILD_TIMEOUT_SECONDS = 259200  # 3 days
ANALYSIS_BASE_TIMEOUT_SECONDS = 600


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _terminal_statuses() -> list[str]:
    return [
        TaskStatus.SUCCESS,
        TaskStatus.SKIPPED,
        TaskStatus.FAILED,
        TaskStatus.TIMEOUT,
        TaskStatus.CANCELLED,
    ]


def _dispatch_hot_builds(db: Session) -> list[dict]:
    """Atomically queue one hot build per source without publishing duplicates."""
    sources = (
        db.query(VideoSource)
        .filter(VideoSource.enabled.is_(True), VideoSource.source_paused.is_(False))
        .all()
    )
    dispatched: list[dict] = []
    dispatcher = get_container().dispatcher
    for source in sources:
        try:
            task_id = dispatcher.dispatch_session_build(
                SessionBuildCommand(source_id=source.id, scan_mode=ScanMode.HOT)
            )
            dispatched.append({"source_id": source.id, "task_id": task_id})
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
            return settings.ANALYSIS_PROGRESS_GRACE_SECONDS
        checkpoint_count = (
            db.query(SessionAnalysisCheckpoint)
            .filter(
                SessionAnalysisCheckpoint.session_id == task_log.task_target_id,
                SessionAnalysisCheckpoint.state == "success",
            )
            .count()
        )
        attempt_count = (
            db.query(SessionAnalysisCheckpoint)
            .filter(SessionAnalysisCheckpoint.session_id == task_log.task_target_id)
            .with_entities(SessionAnalysisCheckpoint.attempt_count)
            .all()
        )
        retry_grace = max((row[0] for row in attempt_count), default=0) * 60
        progress_grace = settings.ANALYSIS_PROGRESS_GRACE_SECONDS + retry_grace
        if checkpoint_count:
            return max(settings.ANALYSIS_PROGRESS_GRACE_SECONDS, progress_grace)
        return max(ANALYSIS_BASE_TIMEOUT_SECONDS, progress_grace)

    if task_log.task_type == TaskType.DAILY_SUMMARY_GENERATION:
        return 300

    return ANALYSIS_BASE_TIMEOUT_SECONDS


def _analysis_last_progress_at(db: Session, session_id: int) -> datetime | None:
    row = (
        db.query(SessionAnalysisCheckpoint.completed_at)
        .filter(
            SessionAnalysisCheckpoint.session_id == session_id,
            SessionAnalysisCheckpoint.state == "success",
            SessionAnalysisCheckpoint.completed_at.is_not(None),
        )
        .order_by(SessionAnalysisCheckpoint.completed_at.desc())
        .first()
    )
    return _as_utc(row[0]) if row is not None else None


def _resume_lost_analysis(db: Session, task_log: TaskLog, now: datetime) -> bool:
    if task_log.task_target_id is None or task_log.cancel_requested:
        return False
    next_attempt = task_log.recovery_attempt + 1
    session = db.query(VideoSession).filter(VideoSession.id == task_log.task_target_id).first()
    if next_attempt > settings.ANALYSIS_RECOVERY_MAX_ATTEMPTS:
        claimed = transition_task_log(
            db,
            task_log.id,
            TaskStatus.RUNNING,
            TaskStatus.TIMEOUT,
            reason="analysis_recovery_limit_reached",
            source="resume_lost_analysis",
        )
        if not claimed.applied:
            db.rollback()
            return False
        task_log.finished_at = now
        task_log.message = f"Automatic recovery limit reached for session {task_log.task_target_id}"
        task_log.recovery_attempt = next_attempt
        if session is not None and session.analysis_status == SessionAnalysisStatus.ANALYZING:
            transition_session(
                db,
                session.id,
                SessionAnalysisStatus.ANALYZING,
                SessionAnalysisStatus.SEALED,
                reason="analysis_recovery_limit_reached",
                source="resume_lost_analysis",
                task_log=task_log,
            )
        return False

    claimed = transition_task_log(
        db,
        task_log.id,
        TaskStatus.RUNNING,
        TaskStatus.TIMEOUT,
        reason="analysis_lease_expired",
        source="resume_lost_analysis",
    )
    if not claimed.applied:
        db.rollback()
        return False
    task_log.finished_at = now
    task_log.message = f"Analysis worker lease expired; automatic recovery {next_attempt} scheduled"
    task_log.recovery_attempt = next_attempt
    if session is not None and session.analysis_status == SessionAnalysisStatus.ANALYZING:
        transition_session(
            db,
            session.id,
            SessionAnalysisStatus.ANALYZING,
            SessionAnalysisStatus.SEALED,
            reason="analysis_lease_expired",
            source="resume_lost_analysis",
            task_log=task_log,
        )
    db.commit()
    priority = "hot"
    if isinstance(task_log.detail_json, dict):
        priority = str(task_log.detail_json.get("priority") or priority)
    get_container().dispatcher.dispatch_analyze_session(
        AnalyzeSessionCommand(
            session_id=task_log.task_target_id,
            priority=priority,
            recovery_attempt=next_attempt,
        )
    )
    return True


def _recover_timed_out_tasks(db: Session, now: datetime) -> int:
    running_tasks = db.query(TaskLog).filter(TaskLog.status == TaskStatus.RUNNING).all()
    timeout_count = 0

    for item in running_tasks:
        if item.cancel_requested:
            continue
        started_at = _as_utc(item.started_at or item.created_at)
        timeout_seconds = _task_timeout_seconds(db, item)
        last_progress = (
            _analysis_last_progress_at(db, item.task_target_id)
            if item.task_type == TaskType.SESSION_ANALYSIS and item.task_target_id is not None
            else None
        )
        timeout_anchor = _as_utc(last_progress or item.last_heartbeat_at or started_at)
        lease_expired = item.lease_expires_at is not None and _as_utc(item.lease_expires_at) <= now
        if timeout_anchor + timedelta(seconds=timeout_seconds) > now or not lease_expired:
            continue

        if item.queue_task_id:
            try:
                celery_app.control.revoke(item.queue_task_id, terminate=True)
            except Exception:
                logger.exception("Failed to revoke timed-out task_id=%s", item.queue_task_id)

        if item.task_type == TaskType.SESSION_ANALYSIS:
            _resume_lost_analysis(db, item, now)
        else:
            item.status = TaskStatus.TIMEOUT
            item.message = f"Task timed out after {timeout_seconds} seconds"
            item.finished_at = now

        timeout_count += 1

    return timeout_count


def _recover_unleased_pending_tasks(db: Session, now: datetime) -> int:
    """Time out pending tasks never leased by any worker.

    Covers broker message loss / worker outage. Without this, such a task
    stays pending forever and, for session builds, keeps deferring hot scans.
    Analysis tasks are excluded: they may legitimately queue for long stretches
    on the single-concurrency vision worker.
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
        .all()
    )

    recovered = 0
    for item in pending_tasks:
        if item.cancel_requested or item.task_type == TaskType.SESSION_ANALYSIS:
            continue
        if item.queue_task_id:
            # Revoke so a late broker delivery cannot re-bind and resurrect
            # the timed-out task log.
            try:
                celery_app.control.revoke(item.queue_task_id, terminate=True)
            except Exception:
                logger.exception("Failed to revoke unleased task_id=%s", item.queue_task_id)
        item.status = TaskStatus.TIMEOUT
        item.finished_at = now
        item.message = "Pending task was never picked up by a worker; timed out"
        recovered += 1

    return recovered


def _recover_orphan_pending_tasks(db: Session, now: datetime) -> int:
    recovered = _recover_unleased_pending_tasks(db, now)
    stale_before = now - timedelta(seconds=settings.ANALYSIS_PENDING_GRACE_SECONDS)
    pending_tasks = (
        db.query(TaskLog)
        .filter(TaskLog.status == TaskStatus.PENDING, TaskLog.created_at <= stale_before)
        .order_by(TaskLog.created_at.asc())
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
            _resume_lost_analysis(db, item, now)
        else:
            item.status = TaskStatus.TIMEOUT
            item.finished_at = now
            item.message = "Pending task orphaned after expired worker lease"

        recovered += 1

    return recovered


def _mark_missing_video_files(db: Session) -> int:
    """Sweep enabled sources for video files deleted from disk.

    Runs hourly (see heartbeat): the sweep stats every known file, so it is
    too expensive for the per-minute hot build path.
    """
    sources = (
        db.query(VideoSource)
        .filter(VideoSource.enabled.is_(True), VideoSource.source_paused.is_(False))
        .all()
    )
    marked = 0
    for source in sources:
        if source.source_type != SourceType.LOCAL_DIRECTORY:
            continue
        marked += mark_missing_source_video_files(db, source.id)
    return marked


def _cleanup_old_task_logs(db: Session) -> int:
    """Delete task logs older than CLEANUP_DAYS."""
    threshold = datetime.now(timezone.utc) - timedelta(days=CLEANUP_DAYS)
    deleted = (
        db.query(TaskLog)
        .filter(
            TaskLog.created_at < threshold,
            TaskLog.status.in_(_terminal_statuses()),
        )
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def heartbeat(self: Any) -> dict[str, Any]:
    """Main heartbeat: runs every 60s via Celery Beat."""
    with task_db_session() as db:
        now = datetime.now(timezone.utc)
        try:
            # 1. Dispatch hot builds for all enabled sources
            dispatched_hot = _dispatch_hot_builds(db)

            # 2. Recover timed-out tasks
            timeout_count = _recover_timed_out_tasks(db, now)

            # 3. Recover orphan pending tasks
            pending_recovered = _recover_orphan_pending_tasks(db, now)

            # 4. Hourly maintenance: old log cleanup + missing-file sweep
            logs_deleted = 0
            missing_marked = 0
            if now.minute == 0:
                logs_deleted = _cleanup_old_task_logs(db)
                missing_marked = _mark_missing_video_files(db)

            db.commit()
            return {
                "dispatched_hot": len(dispatched_hot),
                "timed_out": timeout_count,
                "pending_recovered": pending_recovered,
                "logs_deleted": logs_deleted,
                "missing_marked": missing_marked,
            }
        except Exception:
            db.rollback()
            logger.exception("Heartbeat failed")
            raise
