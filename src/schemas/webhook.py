from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class WebhookBase(BaseModel):
    name: str
    url: str
    headers_json: Any | None = None
    event_subscriptions_json: list[dict[str, str]] | None = None
    enabled: bool = True


class WebhookCreate(WebhookBase):
    pass


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    headers_json: Any | None = None
    event_subscriptions_json: list[dict[str, str]] | None = None
    enabled: bool | None = None


class WebhookResponse(WebhookBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
