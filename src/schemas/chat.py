from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ChatAskRequest(BaseModel):
    question: str


class ChatAskResponse(BaseModel):
    question: str
    answer_text: str
    referenced_events: list[dict] | None = None
    referenced_sessions: list[dict] | None = None


class ChatQueryLogResponse(BaseModel):
    id: int
    user_question: str
    parsed_condition_json: Any | None = None
    answer_text: str
    referenced_event_ids_json: Any | None = None
    provider_id: int | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
