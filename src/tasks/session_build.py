"""Session build tasks: hot_build_task and full_build_task.

Two Celery tasks sharing the same SessionBuilder service.
hot: heartbeat auto-triggered, scans recent files, short timeout.
full: user-triggered, scans entire history, long timeout.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.core.celery_app import celery_app
from src.db.session import SessionLocal
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    ScanMode,
    SourceType,
    TaskStatus,
    TaskType,
)
from src.services.session_builder import SessionBuilder
from src.services.task_dispatch_control import (
    TaskCancellationRequested,
    bind_or_create_running_task_log,
    ensure_task_not_cancelled,
    finalize_cancelled_task_log,
    finalize_task_log,
    get_task_log_for_update,
)

logger = logging.getLogger(__name__)

HOT_WINDOW_HOURS = 24

_session_builder = SessionBuilder()


def _make_cancel_check(db: Session, task_log_id: int, source_id: int):
    def _cancel_check() -> None:
        ensure_task_not_cancelled(
            db,
            task_log_id,
            default_message=f"Build cancelled for source {source_id}",
        )

    return _cancel_check


def _get_latest_session_end_time(db: Session, source_id: int) -> datetime | None:
    """Get the latest session_end_time across all sessions for a source."""
    row = (
        db.query(VideoSession.session_end_time)
        .filter(VideoSession.source_id == source_id)
        .order_by(VideoSession.session_end_time.desc())
        .first()
    )
    if row is None:
        return None
    return row[0]


def _compute_hot_scan_start(db: Session, source_id: int, now: datetime) -> datetime:
    """Hot scan start = max(now - 24h, latest session end time)."""
    window_start = now - timedelta(hours=HOT_WINDOW_HOURS)
    latest_end = _get_latest_session_end_time(db, source_id)
    if latest_end is None:
        return window_start
    return max(window_start, latest_end)


def _compute_full_scan_end(now: datetime) -> datetime:
    """Full scan end uses a stable hot boundary fixed at task start."""
    return now - timedelta(hours=HOT_WINDOW_HOURS)


def _dispatch_analysis_for_sealed(
    sealed_sessions: list,
) -> list[dict]:
    """Dispatch analysis tasks for sealed sessions via Celery send_task."""
    dispatched: list[dict] = []
    for info in sealed_sessions:
        queue = "analysis_hot" if info.priority == ScanMode.HOT else "analysis_full"
        try:
            task = celery_app.send_task(
                "src.tasks.analyzer.analyze_session_task",
                args=[info.session_id],
                kwargs={"priority": info.priority},
                queue=queue,
            )
            dispatched.append(
                {
                    "session_id": info.session_id,
                    "task_id": str(task.id),
                    "queue": queue,
                }
            )
        except Exception:
            logger.exception("Failed to dispatch analysis for session %s", info.session_id)
    return dispatched


@celery_app.task(bind=True, time_limit=3600)
def hot_build_task(self, source_id: int) -> dict:
    """Hot session build: scan recent files, merge into sessions."""
    db: Session = SessionLocal()
    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    task_log = bind_or_create_running_task_log(
        db,
        queue_task_id=queue_task_id or None,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source_id,
        detail_json={"scan_mode": ScanMode.HOT, "source_id": source_id},
    )
    db.commit()

    try:
        cancel_check = _make_cancel_check(db, task_log.id, source_id)
        cancel_check()
        source = db.query(VideoSource).filter(VideoSource.id == source_id).first()
        if not source:
            raise ValueError(f"Video source {source_id} not found")
        if source.source_paused:
            finalize_task_log(task_log, TaskStatus.SUCCESS, "Video source paused, build skipped")
            db.commit()
            return {"skipped": True, "reason": "paused"}

        if source.source_type != SourceType.LOCAL_DIRECTORY:
            raise ValueError(f"Unsupported source type: {source.source_type}")

        config = source.config_json or {}
        root_path = config.get("root_path")
        if not root_path:
            raise ValueError(f"root_path not configured for source {source_id}")

        now = datetime.now()
        scan_start = _compute_hot_scan_start(db, source_id, now)

        build_result = _session_builder.build(
            db=db,
            source_id=source_id,
            root_path=root_path,
            scan_mode=ScanMode.HOT,
            scan_start=scan_start,
            scan_end=now,
            cancel_check=cancel_check,
        )

        source.last_scan_at = now

        finalize_task_log(
            task_log,
            TaskStatus.SUCCESS,
            (
                f"Hot build: found {build_result.files_found}, "
                f"inserted {build_result.files_inserted}, "
                f"skipped {build_result.files_skipped}, "
                f"created {build_result.sessions_created} sessions, "
                f"sealed {build_result.sessions_sealed}"
            ),
            {
                "scan_mode": ScanMode.HOT,
                "source_id": source_id,
                "scan_start": scan_start.isoformat(),
                "scan_end": now.isoformat(),
                "files_found": build_result.files_found,
                "files_inserted": build_result.files_inserted,
                "files_skipped": build_result.files_skipped,
                "sessions_created": build_result.sessions_created,
                "sessions_sealed": build_result.sessions_sealed,
            },
        )
        db.commit()

        # Dispatch analysis for sealed sessions
        dispatched = _dispatch_analysis_for_sealed(build_result.sealed_sessions)

        return {
            "files_found": build_result.files_found,
            "files_inserted": build_result.files_inserted,
            "sessions_created": build_result.sessions_created,
            "sessions_sealed": build_result.sessions_sealed,
            "analysis_dispatched": len(dispatched),
        }

    except TaskCancellationRequested as exc:
        logger.info("Hot build task cancelled for source %s", source_id)
        db.rollback()
        refreshed_task_log = get_task_log_for_update(db, task_log.id)
        if refreshed_task_log is None:
            raise
        finalize_cancelled_task_log(
            refreshed_task_log,
            str(exc),
            {"scan_mode": ScanMode.HOT, "source_id": source_id, "cancelled": True},
        )
        db.commit()
        return {"cancelled": True, "source_id": source_id, "scan_mode": ScanMode.HOT}

    except Exception as e:
        logger.exception("Failed hot build for source %s", source_id)
        db.rollback()
        finalize_task_log(task_log, TaskStatus.FAILED, str(e))
        db.commit()
        raise
    finally:
        db.close()


@celery_app.task(bind=True, time_limit=259200)  # 3 days
def full_build_task(self, source_id: int) -> dict:
    """Full session build: scan entire history up to hot boundary."""
    db: Session = SessionLocal()
    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    task_log = bind_or_create_running_task_log(
        db,
        queue_task_id=queue_task_id or None,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source_id,
        detail_json={"scan_mode": ScanMode.FULL, "source_id": source_id},
    )
    db.commit()

    try:
        cancel_check = _make_cancel_check(db, task_log.id, source_id)
        cancel_check()
        source = db.query(VideoSource).filter(VideoSource.id == source_id).first()
        if not source:
            raise ValueError(f"Video source {source_id} not found")

        if source.source_type != SourceType.LOCAL_DIRECTORY:
            raise ValueError(f"Unsupported source type: {source.source_type}")

        config = source.config_json or {}
        root_path = config.get("root_path")
        if not root_path:
            raise ValueError(f"root_path not configured for source {source_id}")

        from src.adapters.xiaomi_parser import XiaomiDirectoryParser

        parser = XiaomiDirectoryParser(root_path)
        earliest_folder_time, _ = parser.get_directory_time_bounds()
        if not earliest_folder_time:
            finalize_task_log(task_log, TaskStatus.SUCCESS, "No video directories found")
            db.commit()
            return {"skipped": True, "reason": "no_directories"}

        now = datetime.now()
        hot_boundary = _compute_full_scan_end(now)

        # Full scans from earliest to hot boundary
        scan_start = earliest_folder_time - timedelta(hours=1)
        scan_end = hot_boundary

        if scan_start >= scan_end:
            finalize_task_log(
                task_log,
                TaskStatus.SUCCESS,
                "Full build: no time range to scan (hot already covers all)",
            )
            db.commit()
            return {"skipped": True, "reason": "no_range"}

        build_result = _session_builder.build(
            db=db,
            source_id=source_id,
            root_path=root_path,
            scan_mode=ScanMode.FULL,
            scan_start=scan_start,
            scan_end=scan_end,
            cancel_check=cancel_check,
        )

        finalize_task_log(
            task_log,
            TaskStatus.SUCCESS,
            (
                f"Full build: found {build_result.files_found}, "
                f"inserted {build_result.files_inserted}, "
                f"skipped {build_result.files_skipped}, "
                f"created {build_result.sessions_created} sessions, "
                f"sealed {build_result.sessions_sealed}"
            ),
            {
                "scan_mode": ScanMode.FULL,
                "source_id": source_id,
                "scan_start": scan_start.isoformat(),
                "scan_end": scan_end.isoformat(),
                "files_found": build_result.files_found,
                "files_inserted": build_result.files_inserted,
                "files_skipped": build_result.files_skipped,
                "sessions_created": build_result.sessions_created,
                "sessions_sealed": build_result.sessions_sealed,
            },
        )
        db.commit()

        # Dispatch analysis for sealed sessions
        dispatched = _dispatch_analysis_for_sealed(build_result.sealed_sessions)

        return {
            "files_found": build_result.files_found,
            "files_inserted": build_result.files_inserted,
            "sessions_created": build_result.sessions_created,
            "sessions_sealed": build_result.sessions_sealed,
            "analysis_dispatched": len(dispatched),
        }

    except TaskCancellationRequested as exc:
        logger.info("Full build task cancelled for source %s", source_id)
        db.rollback()
        refreshed_task_log = get_task_log_for_update(db, task_log.id)
        if refreshed_task_log is None:
            raise
        finalize_cancelled_task_log(
            refreshed_task_log,
            str(exc),
            {"scan_mode": ScanMode.FULL, "source_id": source_id, "cancelled": True},
        )
        db.commit()
        return {"cancelled": True, "source_id": source_id, "scan_mode": ScanMode.FULL}

    except Exception as e:
        logger.exception("Failed full build for source %s", source_id)
        db.rollback()
        finalize_task_log(task_log, TaskStatus.FAILED, str(e))
        db.commit()
        raise
    finally:
        db.close()
