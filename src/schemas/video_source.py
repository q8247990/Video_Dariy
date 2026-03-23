from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class VideoSourceBase(BaseModel):
    source_name: str
    camera_name: str
    location_name: str
    description: Optional[str] = None
    prompt_text: Optional[str] = None
    source_type: str
    config_json: Optional[Any] = None
    enabled: bool = True


class VideoSourceCreate(VideoSourceBase):
    pass


class VideoSourceUpdate(BaseModel):
    source_name: Optional[str] = None
    camera_name: Optional[str] = None
    location_name: Optional[str] = None
    description: Optional[str] = None
    prompt_text: Optional[str] = None
    source_type: Optional[str] = None
    config_json: Optional[Any] = None
    enabled: Optional[bool] = None


class VideoPathValidateRequest(BaseModel):
    path: str


class VideoPathValidateResponse(BaseModel):
    valid: bool
    file_count: int = 0
    latest_file_time: Optional[datetime] = None
    earliest_file_time: Optional[datetime] = None
    message: str


class VideoSourceResponse(VideoSourceBase):
    id: int
    source_paused: bool = False
    paused_at: Optional[datetime] = None
    last_scan_at: Optional[datetime] = None
    last_validate_status: Optional[str] = None
    last_validate_message: Optional[str] = None
    last_validate_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VideoSourceStatusResponse(BaseModel):
    source_id: int
    video_earliest_time: Optional[datetime] = None
    video_latest_time: Optional[datetime] = None
    analyzed_earliest_time: Optional[datetime] = None
    analyzed_latest_time: Optional[datetime] = None
    analyzed_coverage_percent: Optional[float] = None
    analysis_state: str
    minutes_since_last_new_video: Optional[int] = None
    full_build_running: bool = False
    updated_at: datetime
