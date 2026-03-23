from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class WebhookBase(BaseModel):
    name: str
    url: str
    headers_json: Optional[Any] = None
    event_subscriptions_json: Optional[list[dict[str, str]]] = None
    event_types_json: Optional[list[str]] = None
    enabled: bool = True


class WebhookCreate(WebhookBase):
    pass


class WebhookUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    headers_json: Optional[Any] = None
    event_subscriptions_json: Optional[list[dict[str, str]]] = None
    event_types_json: Optional[list[str]] = None
    enabled: Optional[bool] = None


class WebhookResponse(WebhookBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
