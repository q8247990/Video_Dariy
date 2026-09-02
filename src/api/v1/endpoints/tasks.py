import logging
from typing import Any

from fastapi import APIRouter
from kombu.exceptions import OperationalError

from src.api.deps import DB, ContainerDep, CurrentUser, Locale, Orchestrator
from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SessionBuildCommand,
)
from src.application.tasks import (
    RetryTaskLogResult,
    StopTaskLogResult,
    retry_task_log_use_case,
    stop_task_log_use_case,
)
from src.core.i18n import t
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
def delete_task_log(db: DB, current_user: CurrentUser, locale: Locale, id: int) -> Any:
    row = db.query(TaskLog).filter(TaskLog.id == id).first()
    if not row:
        return BaseResponse(code=4002, message=t("task.log_not_found", locale))

    if row.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
        return BaseResponse(code=4004, message=t("task.cannot_delete_active", locale))

    db.delete(row)
    db.commit()
    return BaseResponse(data={})


@router.post("/logs/{id}/stop", response_model=BaseResponse[dict])
def stop_task_log(
    db: DB,
    current_user: CurrentUser,
    locale: Locale,
    container: ContainerDep,
    id: int,
) -> Any:
    result: StopTaskLogResult = stop_task_log_use_case(
        db=db,
        task_log_id=id,
        locale=locale,
        container=container,
    )
    if result.error_code != 0:
        return BaseResponse(code=result.error_code, message=result.error_message)
    db.commit()
    return BaseResponse(data=result.payload)


@router.post("/logs/{id}/retry", response_model=BaseResponse[dict])
def retry_task_log(
    db: DB,
    current_user: CurrentUser,
    locale: Locale,
    container: ContainerDep,
    id: int,
) -> Any:
    result: RetryTaskLogResult = retry_task_log_use_case(
        db=db,
        task_log_id=id,
        locale=locale,
        container=container,
    )
    if result.error_code != 0:
        data = {"task_id": result.task_id} if result.task_id else None
        return BaseResponse(code=result.error_code, message=result.error_message, data=data)
    db.commit()
    return BaseResponse(data={"task_id": result.task_id})


@router.post("/{id}/build/full", response_model=BaseResponse[dict])
def trigger_full_build(
    db: DB, current_user: CurrentUser, locale: Locale, orchestrator: Orchestrator, id: int
) -> Any:
    """Trigger a full session build for a video source."""
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if source is None:
        return BaseResponse(code=4002, message=t("source.not_found", locale))
    if not source.enabled:
        return BaseResponse(code=4004, message=t("task.source_disabled", locale))

    if is_singleton_task_running(db, TaskType.SESSION_BUILD, id, scan_mode=ScanMode.FULL):
        return BaseResponse(code=4004, message=t("task.full_build_running", locale))

    try:
        task_id = orchestrator.dispatch_session_build(
            SessionBuildCommand(source_id=id, scan_mode=ScanMode.FULL)
        )
    except OperationalError as e:
        logger.exception("Failed to enqueue full build task for source_id=%s", id)
        return BaseResponse(code=5001, message=t("task.queue_unavailable", locale, error=e))
    return BaseResponse(data={"task_id": task_id})


@router.post("/analyze/{session_id}", response_model=BaseResponse[dict])
def trigger_analyze(
    db: DB, current_user: CurrentUser, locale: Locale, orchestrator: Orchestrator, session_id: int
) -> Any:
    """Trigger AI analysis for a sealed video session."""
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if session is None:
        return BaseResponse(code=4002, message=t("session.not_found", locale))
    if session.analysis_status == SessionAnalysisStatus.ANALYZING:
        return BaseResponse(code=4004, message=t("session.is_analyzing", locale))
    if session.analysis_status == SessionAnalysisStatus.OPEN:
        return BaseResponse(code=4004, message=t("session.is_open", locale))

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
        return BaseResponse(code=5001, message=t("task.queue_unavailable", locale, error=e))
    return BaseResponse(data={"task_id": task_id})


@router.post("/summarize", response_model=BaseResponse[dict])
def trigger_summarize(
    db: DB,
    current_user: CurrentUser,
    locale: Locale,
    orchestrator: Orchestrator,
    target_date: str | None = None,
) -> Any:
    """Trigger daily summary generation. Date format: YYYY-MM-DD"""
    try:
        task_id = orchestrator.dispatch_generate_daily_summary(
            GenerateDailySummaryCommand(target_date_str=target_date)
        )
    except OperationalError as e:
        logger.exception("Failed to enqueue summarize task for target_date=%s", target_date)
        return BaseResponse(code=5001, message=t("task.queue_unavailable", locale, error=e))
    return BaseResponse(data={"task_id": task_id})
