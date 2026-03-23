from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict

from src.schemas.tag import TagResponse


class EventBase(BaseModel):
    source_id: int
    session_id: int
    event_start_time: datetime
    event_end_time: Optional[datetime] = None
    object_type: Optional[str] = None
    action_type: Optional[str] = None
    description: str
    confidence_score: Optional[float] = None
    event_type: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    detail: Optional[str] = None
    importance_level: Optional[str] = None
    offset_start_sec: Optional[float] = None
    offset_end_sec: Optional[float] = None
    related_entities_json: Optional[Any] = None
    observed_actions_json: Optional[Any] = None
    interpreted_state_json: Optional[Any] = None
    raw_result: Optional[Any] = None


class EventCreate(EventBase):
    pass


class EventResponse(EventBase):
    id: int
    session_start_time: Optional[datetime] = None
    session_total_duration_seconds: Optional[int] = None
    session_analysis_status: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    # Optional field if we attach tags later
    tags: Optional[List[TagResponse]] = None

    model_config = ConfigDict(from_attributes=True)


class EventDetailResponse(EventResponse):
    source_name: str
    camera_name: str
    location_name: str
