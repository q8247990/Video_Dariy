from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class VideoSourceBase(BaseModel):
    source_name: str
    camera_name: str
    location_name: str
    description: str | None = None
    prompt_text: str | None = None
    source_type: str
    config_json: Any | None = None
    enabled: bool = True


class VideoSourceCreate(VideoSourceBase):
    pass


class VideoSourceUpdate(BaseModel):
    source_name: str | None = None
    camera_name: str | None = None
    location_name: str | None = None
    description: str | None = None
    prompt_text: str | None = None
    source_type: str | None = None
    config_json: Any | None = None
    enabled: bool | None = None


class VideoPathValidateRequest(BaseModel):
    path: str


class VideoPathValidateResponse(BaseModel):
    valid: bool
    file_count: int = 0
    latest_file_time: datetime | None = None
    earliest_file_time: datetime | None = None
    message: str


class VideoSourceResponse(VideoSourceBase):
    id: int
    source_paused: bool = False
    paused_at: datetime | None = None
    last_scan_at: datetime | None = None
    last_validate_status: str | None = None
    last_validate_message: str | None = None
    last_validate_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VideoSourceStatusResponse(BaseModel):
    source_id: int
    video_earliest_time: datetime | None = None
    video_latest_time: datetime | None = None
    analyzed_earliest_time: datetime | None = None
    analyzed_latest_time: datetime | None = None
    analyzed_coverage_percent: float | None = None
    analysis_state: str
    minutes_since_last_new_video: int | None = None
    full_build_running: bool = False
    updated_at: datetime
