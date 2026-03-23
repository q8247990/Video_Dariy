from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class DailySummaryBase(BaseModel):
    summary_date: date
    summary_title: Optional[str] = None
    overall_summary: Optional[str] = None
    subject_sections_json: Optional[list[dict[str, Any]]] = None
    attention_items_json: Optional[list[dict[str, Any]]] = None
    event_count: int = 0
    provider_id: Optional[int] = None


class DailySummaryCreate(DailySummaryBase):
    pass


class DailySummaryResponse(DailySummaryBase):
    id: int
    generated_at: datetime
    detail_text: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
