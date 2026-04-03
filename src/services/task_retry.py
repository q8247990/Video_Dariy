import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SessionBuildCommand,
)
from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    ScanMode,
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
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
    orchestrator: PipelineOrchestrator,
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
        return _retry_session_build(db, task_log, detail, orchestrator)
    elif task_log.task_type == TaskType.SESSION_ANALYSIS:
        return _retry_session_analysis(db, task_log, detail, orchestrator)
    elif task_log.task_type == TaskType.DAILY_SUMMARY_GENERATION:
        return _retry_daily_summary(detail, orchestrator)
    else:
        return RetryResult(
            success=False,
            error_code=4004,
            error_message="Task type does not support retry",
        )


def _retry_session_build(
    db: Session,
    task_log: TaskLog,
    detail: dict,
    orchestrator: PipelineOrchestrator,
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
    task_id = orchestrator.dispatch_session_build(
        SessionBuildCommand(source_id=task_log.task_target_id, scan_mode=scan_mode)
    )
    return RetryResult(success=True, task_id=task_id)


def _retry_session_analysis(
    db: Session,
    task_log: TaskLog,
    detail: dict,
    orchestrator: PipelineOrchestrator,
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

    if session.analysis_status in (SessionAnalysisStatus.FAILED, SessionAnalysisStatus.SUCCESS):
        session.analysis_status = SessionAnalysisStatus.SEALED
        db.flush()

    priority = str(detail.get("priority") or session.analysis_priority or "hot")
    task_id = orchestrator.dispatch_analyze_session(
        AnalyzeSessionCommand(session_id=task_log.task_target_id, priority=priority)
    )
    return RetryResult(success=True, task_id=task_id)


def _retry_daily_summary(
    detail: dict,
    orchestrator: PipelineOrchestrator,
) -> RetryResult:
    target_date = detail.get("target_date")
    task_id = orchestrator.dispatch_generate_daily_summary(
        GenerateDailySummaryCommand(
            target_date_str=str(target_date) if target_date is not None else None
        )
    )
    return RetryResult(success=True, task_id=task_id)
