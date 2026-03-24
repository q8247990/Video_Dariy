from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class LLMProviderBase(BaseModel):
    provider_name: str
    api_base_url: str
    model_name: str
    timeout_seconds: int = 60
    retry_count: int = 3
    extra_config_json: Optional[Any] = None
    enabled: bool = True
    supports_vision: bool = False
    supports_qa: bool = True
    is_default_vision: bool = False
    is_default_qa: bool = False


class LLMProviderCreate(LLMProviderBase):
    api_key: str


class LLMProviderUpdate(LLMProviderBase):
    api_key: Optional[str] = None


class LLMProviderResponse(LLMProviderBase):
    id: int
    availability_status: str = "unknown"
    availability_message: str = "never tested"
    last_test_status: Optional[str] = None
    last_test_message: Optional[str] = None
    last_test_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LLMProviderUsageProviderItem(BaseModel):
    provider_id: int | None
    provider_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class LLMProviderUsageDailyItem(BaseModel):
    date: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    providers: list[LLMProviderUsageProviderItem]
