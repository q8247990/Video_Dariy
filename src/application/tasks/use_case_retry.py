"""Task retry orchestration (moved out of ``src/services`` under Todo 7).

``src/services/task_retry.py`` used to own this logic, which forced the
service layer to import ``src.application.pipeline.commands`` and
``src.application.pipeline.orchestrator`` — a services→application
dependency the architecture boundary tests flagged as a violation.

The logic itself is orchestration, not a pure business rule: it reads a
``TaskLog`` row, re-validates the target aggregate, performs a state
transition when needed, and then hands a command to the async dispatcher.
That is precisely the application layer's job, so the function now lives
here and depends only on
:class:`~src.application.ports.task_dispatcher.TaskDispatcherPort`.

Behaviour is intentionally unchanged from the service version:

* only ``FAILED`` / ``TIMEOUT`` / ``CANCELLED`` rows may be retried;
* active-dedupe collisions return ``4004`` with the duplicate's id;
* ``SESSION_ANALYSIS`` retries first walk the session back to ``SEALED``
  via :func:`src.services.pipeline_state.transition_session`;
* queue / priority / ``scan_mode`` selection comes from the stored
  ``detail_json`` exactly as before.

The caller owns the SQLAlchemy ``Session`` and is responsible for the
commit, matching the existing endpoint/use-case contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SessionBuildCommand,
)
from src.application.ports.task_dispatcher import TaskDispatcherPort
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    ScanMode,
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.services.pipeline_state import transition_session
from src.services.task_dispatch_control import (
    build_dedupe_key,
    ensure_dict_detail,
    find_duplicate_active_task,
)

logger = logging.getLogger(__name__)


@dataclass
class RetryResult:
    success: bool
    task_id: Optional[str] = None
    error_code: int = 0
    error_message: str = ""


def retry_task(
    db: Session,
    task_log: TaskLog,
    dispatcher: TaskDispatcherPort,
) -> RetryResult:
    if task_log.status not in {TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.CANCELLED}:
        return RetryResult(
            success=False,
            error_code=4004,
            error_message="Only failed/timeout/cancelled tasks can be retried",
        )

    detail = ensure_dict_detail(task_log.detail_json)
    dedupe_key = task_log.dedupe_key or build_dedupe_key(
        task_log.task_type,
        task_log.task_target_id,
        detail,
    )
    duplicate = find_duplicate_active_task(
        db,
        task_log.task_type,
        task_log.task_target_id,
        dedupe_key,
    )
    if duplicate:
        return RetryResult(
            success=False,
            error_code=4004,
            error_message="A task with same target/action is already running",
            task_id=duplicate.queue_task_id or str(duplicate.id),
        )

    if task_log.task_type == TaskType.SESSION_BUILD:
        return _retry_session_build(db, task_log, detail, dispatcher)
    elif task_log.task_type == TaskType.SESSION_ANALYSIS:
        return _retry_session_analysis(db, task_log, detail, dispatcher)
    elif task_log.task_type == TaskType.DAILY_SUMMARY_GENERATION:
        return _retry_daily_summary(detail, dispatcher)
    else:
        return RetryResult(
            success=False,
            error_code=4004,
            error_message="Task type does not support retry",
        )


def _retry_session_build(
    db: Session,
    task_log: TaskLog,
    detail: dict[str, Any],
    dispatcher: TaskDispatcherPort,
) -> RetryResult:
    if task_log.task_target_id is None:
        return RetryResult(
            success=False,
            error_code=4004,
            error_message="Invalid build task target",
        )
    source = db.query(VideoSource).filter(VideoSource.id == task_log.task_target_id).first()
    if source is None:
        return RetryResult(success=False, error_code=4002, error_message="Source not found")
    if not source.enabled:
        return RetryResult(success=False, error_code=4004, error_message="Source is disabled")

    scan_mode = str(detail.get("scan_mode") or ScanMode.HOT.value)
    task_id = dispatcher.dispatch_session_build(
        SessionBuildCommand(source_id=task_log.task_target_id, scan_mode=scan_mode)
    )
    return RetryResult(success=True, task_id=task_id)


def _retry_session_analysis(
    db: Session,
    task_log: TaskLog,
    detail: dict[str, Any],
    dispatcher: TaskDispatcherPort,
) -> RetryResult:
    if task_log.task_target_id is None:
        return RetryResult(
            success=False,
            error_code=4004,
            error_message="Invalid analysis task target",
        )
    session = db.query(VideoSession).filter(VideoSession.id == task_log.task_target_id).first()
    if session is None:
        return RetryResult(success=False, error_code=4002, error_message="Session not found")
    if session.analysis_status == SessionAnalysisStatus.OPEN:
        return RetryResult(
            success=False,
            error_code=4004,
            error_message="Session is open and cannot be analyzed yet",
        )

    if session.analysis_status in (
        SessionAnalysisStatus.FAILED,
        SessionAnalysisStatus.PARTIAL,
        SessionAnalysisStatus.SUCCESS,
    ):
        result = transition_session(
            db,
            session.id,
            SessionAnalysisStatus(session.analysis_status),
            SessionAnalysisStatus.SEALED,
            reason="manual_analysis_retry",
            source="retry_session_analysis",
            task_log=task_log,
        )
        if not result.applied:
            return RetryResult(
                success=False,
                error_code=4004,
                error_message="Session state changed before retry",
            )
        db.flush()

    priority = str(detail.get("priority") or session.analysis_priority or "hot")
    task_id = dispatcher.dispatch_analyze_session(
        AnalyzeSessionCommand(session_id=task_log.task_target_id, priority=priority)
    )
    return RetryResult(success=True, task_id=task_id)


def _retry_daily_summary(
    detail: dict[str, Any],
    dispatcher: TaskDispatcherPort,
) -> RetryResult:
    target_date = detail.get("target_date")
    task_id = dispatcher.dispatch_generate_daily_summary(
        GenerateDailySummaryCommand(
            target_date_str=str(target_date) if target_date is not None else None
        )
    )
    return RetryResult(success=True, task_id=task_id)


__all__ = ["RetryResult", "retry_task"]
