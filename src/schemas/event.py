from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.schemas.tag import TagResponse


class EventBase(BaseModel):
    source_id: int
    session_id: int
    event_start_time: datetime
    event_end_time: datetime | None = None
    object_type: str | None = None
    action_type: str | None = None
    description: str
    confidence_score: float | None = None
    event_type: str | None = None
    title: str | None = None
    summary: str | None = None
    detail: str | None = None
    offset_start_sec: float | None = None
    offset_end_sec: float | None = None
    related_entities_json: Any | None = None
    observed_actions_json: Any | None = None
    interpreted_state_json: Any | None = None
    focus_matches_json: Any | None = None
    raw_result: Any | None = None


class EventResponse(EventBase):
    id: int
    session_start_time: datetime | None = None
    session_total_duration_seconds: int | None = None
    session_analysis_status: str | None = None
    created_at: datetime
    updated_at: datetime

    # Optional field if we attach tags later
    tags: list[TagResponse] | None = None

    model_config = ConfigDict(from_attributes=True)


class EventDetailResponse(EventResponse):
    source_name: str
    camera_name: str
    location_name: str
