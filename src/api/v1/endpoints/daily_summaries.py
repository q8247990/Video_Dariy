from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.exc import OperationalError

from src.api.common import paginate
from src.api.deps import DB, CurrentUser, Locale, get_container_dep
from src.application.daily_summary import to_daily_summary_response
from src.application.pipeline.commands import GenerateDailySummaryCommand
from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.core.i18n import t
from src.models.daily_summary import DailySummary
from src.models.video_session import VideoSession
from src.schemas.daily_summary import DailySummaryResponse
from src.schemas.response import BaseResponse, PaginatedResponse
from src.services.pipeline_constants import SessionAnalysisStatus, TaskType
from src.services.task_dispatch_control import build_dedupe_key, find_duplicate_active_task

router = APIRouter()


def _build_orchestrator() -> PipelineOrchestrator:
    """Build the pipeline orchestrator bound to the composition root's dispatcher."""

    return PipelineOrchestrator(dispatcher=get_container_dep().dispatcher)


# Module-level orchestrator seam. Endpoints dispatch through this object so
# tests (and runtime wiring) can substitute the dispatcher by patching
# ``_pipeline_orchestrator.dispatch_*`` without reassigning the FastAPI
# dependency graph.
_pipeline_orchestrator: PipelineOrchestrator = _build_orchestrator()


def get_orchestrator() -> PipelineOrchestrator:
    """FastAPI dependency exposing the module-level orchestrator seam."""

    return _pipeline_orchestrator


Orchestrator = Annotated[PipelineOrchestrator, Depends(get_orchestrator)]


def _has_active_daily_summary_task(db: DB, target_date: date) -> bool:
    dedupe_key = build_dedupe_key(
        TaskType.DAILY_SUMMARY_GENERATION,
        None,
        {"target_date": str(target_date)},
    )
    return (
        find_duplicate_active_task(db, TaskType.DAILY_SUMMARY_GENERATION, None, dedupe_key)
        is not None
    )


@router.get("", response_model=PaginatedResponse[DailySummaryResponse])
def get_daily_summaries(
    db: DB, current_user: CurrentUser, locale: Locale, page: int = 1, page_size: int = 20
) -> Any:
    query = db.query(DailySummary).order_by(DailySummary.summary_date.desc())

    return paginate(
        query,
        page=page,
        page_size=page_size,
        schema=DailySummaryResponse,
        transform=lambda summary: to_daily_summary_response(summary, locale),
    )


@router.post("/generate-all", response_model=BaseResponse[dict])
def generate_all_daily_summaries(
    db: DB, current_user: CurrentUser, locale: Locale, orchestrator: Orchestrator
) -> Any:
    session_end_times = (
        db.query(VideoSession.session_end_time)
        .filter(VideoSession.analysis_status == SessionAnalysisStatus.SUCCESS)
        .order_by(VideoSession.session_end_time.asc())
        .all()
    )
    if not session_end_times:
        return BaseResponse(code=4004, message=t("summary.no_sessions", locale))

    target_dates = sorted({item[0].date() for item in session_end_times if item[0] is not None})
    if not target_dates:
        return BaseResponse(code=4004, message=t("summary.no_sessions", locale))

    earliest_date = target_dates[0]
    latest_date = target_dates[-1]
    queued_task_ids: list[str] = []
    skipped_dates: list[str] = []

    try:
        for current_date in target_dates:
            if _has_active_daily_summary_task(db, current_date):
                skipped_dates.append(str(current_date))
            else:
                task_id = orchestrator.dispatch_generate_daily_summary(
                    db,
                    GenerateDailySummaryCommand(target_date_str=str(current_date)),
                )
                queued_task_ids.append(str(task_id))
        db.commit()
    except OperationalError as exc:
        db.rollback()
        return BaseResponse(code=5001, message=t("task.queue_unavailable", locale, error=exc))

    return BaseResponse(
        data={
            "earliest_date": str(earliest_date),
            "latest_date": str(latest_date),
            "target_dates": [str(item) for item in target_dates],
            "queued_count": len(queued_task_ids),
            "skipped_count": len(skipped_dates),
            "task_ids": queued_task_ids,
            "skipped_dates": skipped_dates,
        }
    )


@router.get("/{date_str}", response_model=BaseResponse[DailySummaryResponse])
def get_daily_summary(db: DB, current_user: CurrentUser, locale: Locale, date_str: str) -> Any:
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return BaseResponse(code=4000, message=t("summary.invalid_date", locale))

    summary = db.query(DailySummary).filter(DailySummary.summary_date == target_date).first()
    if not summary:
        return BaseResponse(code=4002, message=t("summary.not_found", locale))

    payload = to_daily_summary_response(summary, locale)
    return BaseResponse(data=payload)
