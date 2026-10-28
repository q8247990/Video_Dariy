from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DailySummaryBase(BaseModel):
    summary_date: date
    summary_title: str | None = None
    overall_summary: str | None = None
    subject_sections_json: list[dict[str, Any]] | None = None
    attention_items_json: list[dict[str, Any]] | None = None
    event_count: int = 0
    provider_id: int | None = None


class DailySummaryResponse(DailySummaryBase):
    id: int
    generated_at: datetime
    detail_text: str | None = None

    model_config = ConfigDict(from_attributes=True)
