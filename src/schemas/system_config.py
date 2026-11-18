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

    frame_sampler_enabled: bool | None = None
    frame_sampler_url: str | None = None
    frame_sampler_timeout_seconds: int | None = None
    frame_sampler_queue_limit: int | None = None
    frame_sampler_model_path: str | None = None
    frame_sampler_conf_threshold: float | None = None
    frame_sampler_track_high_thresh: float | None = None
    frame_sampler_min_area_ratio: float | None = None
    frame_sampling_session_frame_limit: int | None = None

    model_config = ConfigDict(extra="forbid")
