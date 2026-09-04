from typing import Optional

from pydantic import BaseModel, ConfigDict


class SystemConfigUpdate(BaseModel):
    home_timezone: Optional[str] = None
    daily_summary_schedule: Optional[str] = None
    daily_summary_time: Optional[str] = None
    scan_interval_seconds: Optional[int] = None
    scan_hot_window_hours: Optional[int] = None
    scan_late_tolerance_seconds: Optional[int] = None
    latency_alert_threshold_seconds: Optional[int] = None
    alert_consecutive_required: Optional[int] = None
    alert_notify_cooldown_minutes: Optional[int] = None
    default_session_merge_gap_seconds: Optional[int] = None
    tag_recommendation_enabled: Optional[bool] = None
    mcp_enabled: Optional[bool] = None
    mcp_token: Optional[str] = None
    home_profile_initialized: Optional[bool] = None
    default_locale: Optional[str] = None
    llm_daily_token_quota_global: Optional[int] = None
    llm_daily_token_quota_per_provider: Optional[int] = None
    media_manifest_ttl_seconds: Optional[int] = None
    media_segment_ttl_seconds: Optional[int] = None

    model_config = ConfigDict(extra="forbid")
