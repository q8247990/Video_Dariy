from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class SystemConfigBase(BaseModel):
    config_key: str
    config_value: Optional[Any] = None


class SystemConfigCreate(SystemConfigBase):
    pass


class SystemConfigUpdate(BaseModel):
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


class SystemConfigResponse(SystemConfigBase):
    id: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
