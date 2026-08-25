from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

# The keyframe preprocessing mode is disabled as a product decision: the code
# path is kept in the repository, but it must not be activatable through any
# configuration surface. The API therefore only accepts "raw_mp4".
_VIDEO_PREPROCESS_MODES = {"raw_mp4"}
_KEYFRAME_TARGET_N_MIN = 16
_KEYFRAME_TARGET_N_MAX = 256
_KEYFRAME_JPEG_QUALITY_MIN = 50
_KEYFRAME_JPEG_QUALITY_MAX = 100


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
    supports_tool_calling: bool = False
    is_default_vision: bool = False
    is_default_qa: bool = False
    video_preprocess_mode: str = "raw_mp4"
    video_keyframe_target_n: int = 120
    video_keyframe_jpeg_quality: int = 88

    @field_validator("video_preprocess_mode")
    @classmethod
    def _validate_video_preprocess_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _VIDEO_PREPROCESS_MODES:
            raise ValueError(
                "video_preprocess_mode only accepts 'raw_mp4'; the 'keyframe' "
                f"mode is disabled, got {value!r}"
            )
        return normalized

    @field_validator("video_keyframe_target_n")
    @classmethod
    def _validate_video_keyframe_target_n(cls, value: int) -> int:
        if not (_KEYFRAME_TARGET_N_MIN <= value <= _KEYFRAME_TARGET_N_MAX):
            raise ValueError(
                f"video_keyframe_target_n must be in "
                f"[{_KEYFRAME_TARGET_N_MIN}, {_KEYFRAME_TARGET_N_MAX}], got {value}"
            )
        return value

    @field_validator("video_keyframe_jpeg_quality")
    @classmethod
    def _validate_video_keyframe_jpeg_quality(cls, value: int) -> int:
        if not (_KEYFRAME_JPEG_QUALITY_MIN <= value <= _KEYFRAME_JPEG_QUALITY_MAX):
            raise ValueError(
                f"video_keyframe_jpeg_quality must be in "
                f"[{_KEYFRAME_JPEG_QUALITY_MIN}, {_KEYFRAME_JPEG_QUALITY_MAX}], "
                f"got {value}"
            )
        return value


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
