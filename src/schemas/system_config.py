from pydantic import BaseModel, ConfigDict


class SystemConfigUpdate(BaseModel):
    home_timezone: str | None = None
    daily_summary_schedule: str | None = None
    daily_summary_time: str | None = None
    scan_interval_seconds: int | None = None
    scan_hot_window_hours: int | None = None

    tag_recommendation_enabled: bool | None = None
    mcp_enabled: bool | None = None
    mcp_token: str | None = None
    home_profile_initialized: bool | None = None
    default_locale: str | None = None
    llm_daily_token_quota_global: int | None = None
    llm_daily_token_quota_per_provider: int | None = None
    media_manifest_ttl_seconds: int | None = None
    media_segment_ttl_seconds: int | None = None
    media_entity_image_ttl_seconds: int | None = None

    model_config = ConfigDict(extra="forbid")
