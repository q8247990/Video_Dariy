from datetime import datetime
from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.models.event_record import EventRecord
from src.models.video_session import VideoSession
from src.schemas.event import EventResponse
from src.schemas.response import BaseResponse, PaginatedData, PaginatedResponse, PaginationDetails
from src.schemas.session import VideoSessionResponse

router = APIRouter()


@router.get("", response_model=PaginatedResponse[VideoSessionResponse])
def get_sessions(
    db: DB,
    current_user: CurrentUser,
    page: int = 1,
    page_size: int = 20,
    source_id: int | None = None,
    analysis_status: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> Any:
    query = db.query(VideoSession)
    if source_id:
        query = query.filter(VideoSession.source_id == source_id)
    if analysis_status:
        query = query.filter(VideoSession.analysis_status == analysis_status)
    if start_time:
        query = query.filter(VideoSession.session_start_time >= start_time)
    if end_time:
        query = query.filter(VideoSession.session_start_time <= end_time)

    query = query.order_by(
        VideoSession.session_start_time.desc(),
        VideoSession.updated_at.desc(),
    )

    total = query.count()
    sessions = query.offset((page - 1) * page_size).limit(page_size).all()

    return PaginatedResponse(
        data=PaginatedData(
            list=[VideoSessionResponse.model_validate(s) for s in sessions],
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )


@router.get("/{id}", response_model=BaseResponse[VideoSessionResponse])
def get_session(db: DB, current_user: CurrentUser, id: int) -> Any:
    session = db.query(VideoSession).filter(VideoSession.id == id).first()
    if not session:
        return BaseResponse(code=4002, message="Session not found")

    return BaseResponse(data=VideoSessionResponse.model_validate(session))


@router.get("/{id}/events", response_model=BaseResponse[list[EventResponse]])
def get_session_events(
    db: DB,
    current_user: CurrentUser,
    id: int,
    order: str = "asc",
) -> Any:
    session = db.query(VideoSession).filter(VideoSession.id == id).first()
    if not session:
        return BaseResponse(code=4002, message="Session not found")

    query = db.query(EventRecord).filter(EventRecord.session_id == id)
    if order == "desc":
        query = query.order_by(EventRecord.event_start_time.desc())
    else:
        query = query.order_by(EventRecord.event_start_time.asc())

    events = query.all()
    payload = [
        EventResponse.model_validate(
            {
                **event.__dict__,
                "session_start_time": session.session_start_time,
                "session_total_duration_seconds": session.total_duration_seconds,
                "session_analysis_status": session.analysis_status,
            }
        )
        for event in events
    ]
    return BaseResponse(data=payload)
