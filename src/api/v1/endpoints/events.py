from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.models.event_record import EventRecord
from src.models.event_tag_rel import EventTagRel
from src.models.tag_definition import TagDefinition
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.schemas.event import EventDetailResponse, EventResponse
from src.schemas.response import BaseResponse, PaginatedData, PaginatedResponse, PaginationDetails

router = APIRouter()


@router.get("", response_model=PaginatedResponse[EventResponse])
def get_events(
    db: DB,
    current_user: CurrentUser,
    page: int = 1,
    page_size: int = 20,
    source_id: int | None = None,
    object_type: str | None = None,
    action_type: str | None = None,
    analysis_status: str | None = None,
) -> Any:
    query = db.query(EventRecord, VideoSession).join(
        VideoSession, EventRecord.session_id == VideoSession.id
    )
    if source_id:
        query = query.filter(EventRecord.source_id == source_id)
    if object_type:
        query = query.filter(EventRecord.object_type == object_type)
    if action_type:
        query = query.filter(EventRecord.action_type == action_type)
    if analysis_status:
        query = query.filter(VideoSession.analysis_status == analysis_status)

    query = query.order_by(EventRecord.event_start_time.desc())

    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    return PaginatedResponse(
        data=PaginatedData(
            list=[
                EventResponse.model_validate(
                    {
                        **event.__dict__,
                        "session_start_time": session.session_start_time,
                        "session_total_duration_seconds": session.total_duration_seconds,
                        "session_analysis_status": session.analysis_status,
                    }
                )
                for event, session in rows
            ],
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )


@router.get("/{id}", response_model=BaseResponse[EventDetailResponse])
def get_event_detail(db: DB, current_user: CurrentUser, id: int) -> Any:
    row = (
        db.query(EventRecord, VideoSession, VideoSource)
        .join(VideoSession, EventRecord.session_id == VideoSession.id)
        .join(VideoSource, EventRecord.source_id == VideoSource.id)
        .filter(EventRecord.id == id)
        .first()
    )
    if not row:
        return BaseResponse(code=4002, message="Event not found")

    event, session, source = row

    tags = (
        db.query(TagDefinition)
        .join(EventTagRel, EventTagRel.tag_id == TagDefinition.id)
        .filter(EventTagRel.event_id == event.id)
        .order_by(TagDefinition.id.asc())
        .all()
    )

    payload = {
        **event.__dict__,
        "session_start_time": session.session_start_time,
        "session_total_duration_seconds": session.total_duration_seconds,
        "session_analysis_status": session.analysis_status,
        "source_name": source.source_name,
        "camera_name": source.camera_name,
        "location_name": source.location_name,
        "tags": tags,
    }

    return BaseResponse(data=EventDetailResponse.model_validate(payload))
