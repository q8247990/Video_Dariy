from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict


class ChatAskRequest(BaseModel):
    question: str


class ChatAskResponse(BaseModel):
    question: str
    answer_text: str
    referenced_events: Optional[List[dict]] = None
    referenced_sessions: Optional[List[dict]] = None


class ChatQueryLogResponse(BaseModel):
    id: int
    user_question: str
    parsed_condition_json: Optional[Any] = None
    answer_text: str
    referenced_event_ids_json: Optional[Any] = None
    provider_id: Optional[int] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
