from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from src.services.provider_key_crypto import mask_provider_api_key


class LLMProviderBase(BaseModel):
    # The keyframe preprocessing mode was removed as a product decision
    # (ADR ``0009-remove-keyframe-pipeline.md``); the column-level
    # configuration surface (video_preprocess_mode /
    # video_keyframe_target_n / video_keyframe_jpeg_quality) was retired
    # by migration ``20260902_0018``. Legacy payloads that still carry
    # those keys are silently ignored (``extra='ignore'``) so existing
    # clients do not start failing after the upgrade, but the capability
    # can never be reactivated through this surface.
    model_config = ConfigDict(extra="ignore")

    provider_name: str
    api_base_url: str
    model_name: str
    timeout_seconds: int = 60
    retry_count: int = 3
    extra_config_json: Optional[Any] = None
    enabled: bool = True
    supports_vision: bool = False
    supports_qa: bool = True
    supports_tool_calling: bool = False
    is_default_vision: bool = False
    is_default_qa: bool = False


class LLMProviderCreate(LLMProviderBase):
    api_key: str


class LLMProviderUpdate(LLMProviderBase):
    api_key: Optional[str] = None


class LLMProviderResponse(LLMProviderBase):
    id: int
    api_key: str
    availability_status: str = "unknown"
    availability_message: str = "never tested"
    last_test_status: Optional[str] = None
    last_test_message: Optional[str] = None
    last_test_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("api_key")
    @classmethod
    def _mask_api_key(cls, value: str) -> str:
        return mask_provider_api_key(value)


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
