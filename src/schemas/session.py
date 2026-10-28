from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class VideoSessionResponse(BaseModel):
    id: int
    source_id: int
    session_start_time: datetime
    session_end_time: datetime
    total_duration_seconds: int | None = None
    merge_rule: str | None = None
    analysis_status: str
    summary_text: str | None = None
    activity_level: str | None = None
    main_subjects_json: Any | None = None
    has_attention_event: bool | None = None
    analysis_notes_json: Any | None = None
    last_analyzed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
