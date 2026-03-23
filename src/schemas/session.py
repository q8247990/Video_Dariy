from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class VideoSessionResponse(BaseModel):
    id: int
    source_id: int
    session_start_time: datetime
    session_end_time: datetime
    total_duration_seconds: Optional[int] = None
    merge_rule: Optional[str] = None
    analysis_status: str
    summary_text: Optional[str] = None
    activity_level: Optional[str] = None
    main_subjects_json: Optional[Any] = None
    has_important_event: Optional[bool] = None
    analysis_notes_json: Optional[Any] = None
    last_analyzed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
