"""Session-build orchestration - the hot/full pipeline body.

The Celery tasks in :mod:`src.tasks.session_build` are thin wrappers
that open a :func:`task_db_session` and delegate the entire pipeline
(TaskLog binding, scan-window policy, the five stages, TaskLog
finalize, analyzer dispatch) to this module so ``hot_build_task`` /
``full_build_task`` each stay a ~10-line delegate. The stages
(discovery / dedupe / reducer / seal policy) all live in
:mod:`src.services.session_build`; the runner in
:mod:`src.services.session_build.runner` returns the
:class:`~src.services.session_build.types.SessionBuildResult`
envelope this module iterates for analyzer dispatch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.adapters.xiaomi_parser import XiaomiDirectoryParser
from src.application.pipeline.commands import AnalyzeSessionCommand
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    AnalysisPriority,
    ScanMode,
    SourceType,
    TaskStatus,
    TaskType,
)
from src.services.session_build.persistence import find_stuck_sealed_sessions
from src.services.session_build.runner import run_full, run_hot
from src.services.session_build.types import SealedSessionInfo
from src.services.system_config_registry import HOME_TIMEZONE, get_config
from src.services.task_dispatch_control import (
    TaskCancellationRequested,
    bind_or_create_running_task_log,
    ensure_task_not_cancelled,
    finalize_cancelled_task_log,
    finalize_task_log,
    get_task_log_for_update,
)
from src.tasks._container import get_container

logger = logging.getLogger(__name__)

HOT_WINDOW_HOURS = 24


# ---------------------------------------------------------------------------
# Pure helpers (hot + full)
# ---------------------------------------------------------------------------


def _make_cancel_check(db: Session, task_log_id: int, source_id: int) -> Callable[[], None]:
    def _inner() -> None:
        ensure_task_not_cancelled(
            db, task_log_id, default_message=f"Build cancelled for source {source_id}"
        )

    return _inner


def _latest_session_end_time(db: Session, source_id: int) -> datetime | None:
    row = (
        db.query(VideoSession.session_end_time)
        .filter(VideoSession.source_id == source_id)
        .order_by(VideoSession.session_end_time.desc())
        .first()
    )
    return None if row is None else row[0]


def _hot_scan_start(db: Session, source_id: int, now: datetime) -> datetime:
    window_start = now - timedelta(hours=HOT_WINDOW_HOURS)
    latest = _latest_session_end_time(db, source_id)
    return window_start if latest is None else max(window_start, latest)


def _compute_full_scan_end(now: datetime) -> datetime:
    """Stable hot boundary used as the FULL-scan upper bound."""
    return now - timedelta(hours=HOT_WINDOW_HOURS)


def _resolve_home_zone(db: Session) -> ZoneInfo:
    return ZoneInfo(str(get_config(db, HOME_TIMEZONE)))


def _load_local_directory_source(db: Session, source_id: int) -> tuple[VideoSource, str]:
    source = db.query(VideoSource).filter(VideoSource.id == source_id).first()
    if source is None:
        raise ValueError(f"Video source {source_id} not found")
    if source.source_type != SourceType.LOCAL_DIRECTORY:
        raise ValueError(f"Unsupported source type: {source.source_type}")
    root_path = (source.config_json or {}).get("root_path")
    if not root_path:
        raise ValueError(f"root_path not configured for source {source_id}")
    return source, str(root_path)


# ---------------------------------------------------------------------------
# Analyzer-dispatch seam - uses the outbox dispatcher, never ``send_task``.
# ---------------------------------------------------------------------------


def _dispatch_analysis_for_sealed(db: Session, sealed_sessions: list) -> list[dict]:
    dispatcher = get_container().dispatcher
    dispatched: list[dict] = []
    for info in sealed_sessions:
        try:
            task_id = dispatcher.dispatch_analyze_session(
                db,
                AnalyzeSessionCommand(session_id=info.session_id, priority=info.priority),
            )
            dispatched.append({"session_id": info.session_id, "task_id": task_id})
        except Exception:
            logger.exception("Failed to dispatch analysis for session %s", info.session_id)
    return dispatched


def _stuck_sealed_envelopes(db: Session, source_id: int) -> list[SealedSessionInfo]:
    """Dispatch envelopes for sealed sessions that lost their analysis handoff.

    Self-heal for the split-commit failure where a build's seal
    transition committed but the analyzer dispatch did not (worker
    death or a swallowed dispatch error): those sessions sit in
    ``SEALED`` with no ``session_analysis`` TaskLog and no other
    mechanism would ever re-dispatch them.
    """
    return [
        SealedSessionInfo(
            session_id=session.id,
            source_id=source_id,
            priority=session.analysis_priority or AnalysisPriority.HOT,
        )
        for session in find_stuck_sealed_sessions(db, source_id)
    ]


# ---------------------------------------------------------------------------
# Shared task body: TaskLog lifecycle + cancel/failure handling.
# ---------------------------------------------------------------------------


def _success_message(scan_mode_label: str, build_result: Any) -> str:
    return (
        f"{scan_mode_label} build: found {build_result.files_found}, "
        f"inserted {build_result.files_inserted}, "
        f"skipped {build_result.files_skipped}, "
        f"created {build_result.sessions_created} sessions, "
        f"sealed {build_result.sessions_sealed}"
    )


def _run_with_task_log_lifecycle(
    db: Session,
    *,
    scan_mode: str,
    scan_mode_label: str,
    source_id: int,
    queue_task_id: str,
    run_builder: Callable[[Session, Callable[[], None]], dict[str, Any]],
) -> dict[str, Any]:
    """Run the TaskLog lifecycle around ``run_builder`` on the caller's ``db``.

    The caller (task entry) owns the ``task_db_session``; this helper
    binds the TaskLog, runs ``run_builder``, finalizes it, and handles
    the shared ``TaskCancellationRequested`` / ``Exception`` envelope.
    """
    task_log = bind_or_create_running_task_log(
        db,
        queue_task_id=queue_task_id or None,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source_id,
        detail_json={"scan_mode": scan_mode, "source_id": source_id},
    )
    if task_log is None:
        logger.warning(
            "Stale %s build message %s for source %s; task log already finalized, skipping",
            scan_mode,
            queue_task_id,
            source_id,
        )
        return {"skipped": True, "reason": "stale_message"}
    db.commit()

    try:
        cancel_check = _make_cancel_check(db, task_log.id, source_id)
        outcome = run_builder(db, cancel_check)
        if "skipped" in outcome:
            return outcome

        build_result = outcome["result"]
        # Sessions this build just sealed are SEALED inside this
        # transaction and carry no TaskLog yet; exclude them so the
        # stuck-sweep count and the dispatch list do not double them.
        fresh_ids = {info.session_id for info in build_result.sealed_sessions}
        stuck_envelopes = [
            envelope
            for envelope in _stuck_sealed_envelopes(db, source_id)
            if envelope.session_id not in fresh_ids
        ]
        finalize_task_log(
            task_log,
            TaskStatus.SUCCESS,
            _success_message(scan_mode_label, build_result),
            {
                "scan_mode": scan_mode,
                "source_id": source_id,
                "scan_start": outcome["scan_start"].isoformat(),
                "scan_end": outcome["scan_end"].isoformat(),
                "files_found": build_result.files_found,
                "files_inserted": build_result.files_inserted,
                "sessions_created": build_result.sessions_created,
                "sessions_sealed": build_result.sessions_sealed,
                "analysis_redispatched": len(stuck_envelopes),
            },
        )
        # Dispatch before the commit: seal and analyzer handoff commit
        # atomically, so a worker death can no longer leave a session
        # SEALED without an analysis TaskLog.
        dispatched = _dispatch_analysis_for_sealed(
            db, [*build_result.sealed_sessions, *stuck_envelopes]
        )
        db.commit()
        return {
            "files_found": build_result.files_found,
            "files_inserted": build_result.files_inserted,
            "sessions_created": build_result.sessions_created,
            "sessions_sealed": build_result.sessions_sealed,
            "analysis_dispatched": len(dispatched),
            "analysis_redispatched": len(stuck_envelopes),
        }

    except TaskCancellationRequested as exc:
        logger.info("%s build task cancelled for source %s", scan_mode_label, source_id)
        db.rollback()
        refreshed = get_task_log_for_update(db, task_log.id)
        if refreshed is None:
            raise
        finalize_cancelled_task_log(
            refreshed,
            str(exc),
            {"scan_mode": scan_mode, "source_id": source_id, "cancelled": True},
        )
        db.commit()
        return {"cancelled": True, "source_id": source_id, "scan_mode": scan_mode}

    except Exception as exc:
        logger.exception("Failed %s build for source %s", scan_mode_label, source_id)
        db.rollback()
        finalize_task_log(task_log, TaskStatus.FAILED, str(exc))
        db.commit()
        raise


# ---------------------------------------------------------------------------
# Builders: scan-mode-specific window policy
# ---------------------------------------------------------------------------


def _build_hot(db: Session, source_id: int, cancel_check: Callable[[], None]) -> dict[str, Any]:
    """HOT-mode builder: scans ``[now - 24h, now]`` (or since the last session)."""
    source, root_path = _load_local_directory_source(db, source_id)
    if source.source_paused:
        return {"skipped": True, "reason": "paused"}

    now = datetime.now(timezone.utc)
    scan_start = _hot_scan_start(db, source_id, now)
    build_result = run_hot(
        db,
        source_id=source_id,
        root_path=root_path,
        scan_start=scan_start,
        scan_end=now,
        cancel_check=cancel_check,
        home_zone=_resolve_home_zone(db),
    )
    source.last_scan_at = now
    return {"scan_start": scan_start, "scan_end": now, "result": build_result}


def _build_full(db: Session, source_id: int, cancel_check: Callable[[], None]) -> dict[str, Any]:
    """FULL-mode builder: scans ``[earliest folder, hot boundary]``."""
    _, root_path = _load_local_directory_source(db, source_id)
    home_zone = _resolve_home_zone(db)
    parser = XiaomiDirectoryParser(root_path, timezone=home_zone)
    earliest_folder_time, _ = parser.get_directory_time_bounds()
    if not earliest_folder_time:
        return {"skipped": True, "reason": "no_directories"}

    now = datetime.now(timezone.utc)
    scan_end = _compute_full_scan_end(now)
    scan_start = earliest_folder_time - timedelta(hours=1)
    if scan_start >= scan_end:
        return {"skipped": True, "reason": "no_range"}

    build_result = run_full(
        db,
        source_id=source_id,
        root_path=root_path,
        scan_start=scan_start,
        scan_end=scan_end,
        cancel_check=cancel_check,
        home_zone=home_zone,
    )
    return {"scan_start": scan_start, "scan_end": scan_end, "result": build_result}


# ---------------------------------------------------------------------------
# Orchestration entry points consumed by the slim Celery tasks.
# ---------------------------------------------------------------------------


def run_hot_build(
    db: Session,
    *,
    source_id: int,
    queue_task_id: str,
) -> dict[str, Any]:
    """HOT-mode orchestration: heartbeat-driven scan of recent files."""
    return _run_with_task_log_lifecycle(
        db,
        scan_mode=ScanMode.HOT,
        scan_mode_label="Hot",
        source_id=source_id,
        queue_task_id=queue_task_id,
        run_builder=lambda db_, cancel_check: _build_hot(db_, source_id, cancel_check),
    )


def run_full_build(
    db: Session,
    *,
    source_id: int,
    queue_task_id: str,
) -> dict[str, Any]:
    """FULL-mode orchestration: user-driven scan to the hot boundary."""
    return _run_with_task_log_lifecycle(
        db,
        scan_mode=ScanMode.FULL,
        scan_mode_label="Full",
        source_id=source_id,
        queue_task_id=queue_task_id,
        run_builder=lambda db_, cancel_check: _build_full(db_, source_id, cancel_check),
    )


__all__ = [
    "HOT_WINDOW_HOURS",
    "_compute_full_scan_end",
    "_dispatch_analysis_for_sealed",
    "run_full_build",
    "run_hot_build",
]
