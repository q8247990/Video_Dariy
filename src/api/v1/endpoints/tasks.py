import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from kombu.exceptions import OperationalError

from src.api.deps import DB, CurrentUser, Orchestrator
from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SessionBuildCommand,
)
from src.core.celery_app import celery_app
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.schemas.response import BaseResponse, PaginatedData, PaginatedResponse, PaginationDetails
from src.schemas.task_log import TaskLogResponse
from src.services.pipeline_constants import (
    ScanMode,
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.services.task_dispatch_control import is_singleton_task_running
from src.services.task_retry import retry_task

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/logs", response_model=PaginatedResponse[TaskLogResponse])
def get_task_logs(
    db: DB,
    current_user: CurrentUser,
    page: int = 1,
    page_size: int = 20,
    task_type: str | None = None,
    status: str | None = None,
) -> Any:
    query = db.query(TaskLog)
    if task_type:
        query = query.filter(TaskLog.task_type == task_type)
    if status:
        query = query.filter(TaskLog.status == status)

    query = query.order_by(TaskLog.created_at.desc())
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    return PaginatedResponse(
        data=PaginatedData(
            list=[TaskLogResponse.model_validate(row) for row in rows],
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )


@router.delete("/logs/{id}", response_model=BaseResponse[dict])
def delete_task_log(db: DB, current_user: CurrentUser, id: int) -> Any:
    row = db.query(TaskLog).filter(TaskLog.id == id).first()
    if not row:
        return BaseResponse(code=4002, message="Task log not found")

    if row.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
        return BaseResponse(code=4004, message="Running or pending task cannot be deleted")

    db.delete(row)
    db.commit()
    return BaseResponse(data={})


@router.post("/logs/{id}/stop", response_model=BaseResponse[dict])
def stop_task_log(db: DB, current_user: CurrentUser, id: int) -> Any:
    row = db.query(TaskLog).filter(TaskLog.id == id).first()
    if not row:
        return BaseResponse(code=4002, message="Task log not found")
    if row.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
        return BaseResponse(code=4004, message="Only pending or running tasks can be stopped")

    if row.queue_task_id:
        try:
            celery_app.control.revoke(row.queue_task_id, terminate=True)
        except Exception as exc:
            logger.exception("Failed to revoke task_id=%s", row.queue_task_id)
            return BaseResponse(code=5001, message=f"Failed to stop task: {exc}")

    row.cancel_requested = True
    if row.status == TaskStatus.PENDING:
        row.status = TaskStatus.CANCELLED
        row.message = "Task cancelled before execution"
        row.finished_at = datetime.now()
    else:
        row.message = "Cancellation requested by user"

    db.commit()
    return BaseResponse(
        data={
            "task_log_id": row.id,
            "status": row.status,
            "cancel_requested": row.cancel_requested,
        }
    )


@router.post("/logs/{id}/retry", response_model=BaseResponse[dict])
def retry_task_log(db: DB, current_user: CurrentUser, orchestrator: Orchestrator, id: int) -> Any:
    row = db.query(TaskLog).filter(TaskLog.id == id).first()
    if not row:
        return BaseResponse(code=4002, message="Task log not found")

    try:
        result = retry_task(db, row, orchestrator)
    except OperationalError as exc:
        logger.exception("Failed to retry task log id=%s", id)
        return BaseResponse(code=5001, message=f"Task queue unavailable: {exc}")

    if not result.success:
        data = {"task_id": result.task_id} if result.task_id else None
        return BaseResponse(code=result.error_code, message=result.error_message, data=data)

    db.commit()
    return BaseResponse(data={"task_id": result.task_id})


@router.post("/{id}/build/full", response_model=BaseResponse[dict])
def trigger_full_build(
    db: DB, current_user: CurrentUser, orchestrator: Orchestrator, id: int
) -> Any:
    """Trigger a full session build for a video source."""
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if source is None:
        return BaseResponse(code=4002, message="Source not found")
    if not source.enabled:
        return BaseResponse(code=4004, message="Source is disabled")

    if is_singleton_task_running(db, TaskType.SESSION_BUILD, id, scan_mode=ScanMode.FULL):
        return BaseResponse(code=4004, message="Full build is already running for this source")

    try:
        task_id = orchestrator.dispatch_session_build(
            SessionBuildCommand(source_id=id, scan_mode=ScanMode.FULL)
        )
    except OperationalError as e:
        logger.exception("Failed to enqueue full build task for source_id=%s", id)
        return BaseResponse(code=5001, message=f"Task queue unavailable: {e}")
    return BaseResponse(data={"task_id": task_id})


@router.post("/analyze/{session_id}", response_model=BaseResponse[dict])
def trigger_analyze(
    db: DB, current_user: CurrentUser, orchestrator: Orchestrator, session_id: int
) -> Any:
    """Trigger AI analysis for a sealed video session."""
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if session is None:
        return BaseResponse(code=4002, message="Session not found")
    if session.analysis_status == SessionAnalysisStatus.ANALYZING:
        return BaseResponse(code=4004, message="Session is analyzing")
    if session.analysis_status == SessionAnalysisStatus.OPEN:
        return BaseResponse(code=4004, message="Session is open and cannot be analyzed yet")

    # For manual retry, reset status to sealed
    if session.analysis_status in (SessionAnalysisStatus.FAILED, SessionAnalysisStatus.SUCCESS):
        session.analysis_status = SessionAnalysisStatus.SEALED
        db.commit()

    priority = session.analysis_priority or "hot"
    try:
        task_id = orchestrator.dispatch_analyze_session(
            AnalyzeSessionCommand(session_id=session_id, priority=priority)
        )
    except OperationalError as e:
        logger.exception("Failed to enqueue analyze task for session_id=%s", session_id)
        return BaseResponse(code=5001, message=f"Task queue unavailable: {e}")
    return BaseResponse(data={"task_id": task_id})


@router.post("/summarize", response_model=BaseResponse[dict])
def trigger_summarize(
    db: DB, current_user: CurrentUser, orchestrator: Orchestrator, target_date: str | None = None
) -> Any:
    """Trigger daily summary generation. Date format: YYYY-MM-DD"""
    try:
        task_id = orchestrator.dispatch_generate_daily_summary(
            GenerateDailySummaryCommand(target_date_str=target_date)
        )
    except OperationalError as e:
        logger.exception("Failed to enqueue summarize task for target_date=%s", target_date)
        return BaseResponse(code=5001, message=f"Task queue unavailable: {e}")
    return BaseResponse(data={"task_id": task_id})
