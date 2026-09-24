"""Typed registry and persistence boundary for runtime system configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from src.models.system_config import SystemConfig

DAILY_SUMMARY_SCHEDULE: Final = "daily_summary_schedule"
DAILY_SUMMARY_TIME: Final = "daily_summary_time"
SCAN_INTERVAL_SECONDS: Final = "scan_interval_seconds"
SCAN_HOT_WINDOW_HOURS: Final = "scan_hot_window_hours"
SCAN_LATE_TOLERANCE_SECONDS: Final = "scan_late_tolerance_seconds"
LATENCY_ALERT_THRESHOLD_SECONDS: Final = "latency_alert_threshold_seconds"
ALERT_CONSECUTIVE_REQUIRED: Final = "alert_consecutive_required"
ALERT_NOTIFY_COOLDOWN_MINUTES: Final = "alert_notify_cooldown_minutes"
TAG_RECOMMENDATION_ENABLED: Final = "tag_recommendation_enabled"
MCP_ENABLED: Final = "mcp_enabled"
MCP_TOKEN: Final = "mcp_token"
HOME_PROFILE_INITIALIZED: Final = "home_profile_initialized"
DEFAULT_LOCALE: Final = "default_locale"
LLM_DAILY_TOKEN_QUOTA_GLOBAL: Final = "llm_daily_token_quota_global"
LLM_DAILY_TOKEN_QUOTA_PER_PROVIDER: Final = "llm_daily_token_quota_per_provider"
MEDIA_MANIFEST_TTL_SECONDS: Final = "media_manifest_ttl_seconds"
MEDIA_SEGMENT_TTL_SECONDS: Final = "media_segment_ttl_seconds"
HOME_TIMEZONE: Final = "home_timezone"


class SystemConfigValidationError(ValueError):
    """Raised when an update is not part of the typed configuration contract."""


@dataclass(frozen=True, slots=True)
class ConfigDefinition:
    """One validated configuration key with a typed default."""

    default: str | int | bool
    parser: Callable[[Any], str | int | bool]


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise SystemConfigValidationError("configuration value must be a string")
    text = value.strip()
    if not text:
        raise SystemConfigValidationError("configuration value must not be blank")
    return text


def _optional_string(value: Any) -> str:
    if not isinstance(value, str):
        raise SystemConfigValidationError("configuration value must be a string")
    return value.strip()


def _iana_timezone(value: Any) -> str:
    timezone_name = _string(value)
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise SystemConfigValidationError("configuration value must be an IANA timezone") from error
    return timezone_name


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise SystemConfigValidationError("configuration value must be a boolean")
    return value


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemConfigValidationError("configuration value must be a positive integer")
    return int(value)


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemConfigValidationError("configuration value must be a non-negative integer")
    return int(value)


REGISTRY: Final[dict[str, ConfigDefinition]] = {
    DAILY_SUMMARY_SCHEDULE: ConfigDefinition("10:00", _string),
    SCAN_INTERVAL_SECONDS: ConfigDefinition(300, _positive_int),
    SCAN_HOT_WINDOW_HOURS: ConfigDefinition(24, _positive_int),
    SCAN_LATE_TOLERANCE_SECONDS: ConfigDefinition(180, _non_negative_int),
    LATENCY_ALERT_THRESHOLD_SECONDS: ConfigDefinition(600, _positive_int),
    ALERT_CONSECUTIVE_REQUIRED: ConfigDefinition(3, _positive_int),
    ALERT_NOTIFY_COOLDOWN_MINUTES: ConfigDefinition(60, _positive_int),
    TAG_RECOMMENDATION_ENABLED: ConfigDefinition(False, _bool),
    MCP_ENABLED: ConfigDefinition(True, _bool),
    MCP_TOKEN: ConfigDefinition("", _optional_string),
    HOME_PROFILE_INITIALIZED: ConfigDefinition(False, _bool),
    DEFAULT_LOCALE: ConfigDefinition("zh-CN", _string),
    LLM_DAILY_TOKEN_QUOTA_GLOBAL: ConfigDefinition(0, _non_negative_int),
    LLM_DAILY_TOKEN_QUOTA_PER_PROVIDER: ConfigDefinition(0, _non_negative_int),
    MEDIA_MANIFEST_TTL_SECONDS: ConfigDefinition(1_800, _positive_int),
    MEDIA_SEGMENT_TTL_SECONDS: ConfigDefinition(1_800, _positive_int),
    HOME_TIMEZONE: ConfigDefinition("Asia/Shanghai", _iana_timezone),
}


def validate_updates(updates: dict[str, Any]) -> dict[str, str | int | bool]:
    """Normalize legacy aliases and parse every API update before persistence."""
    normalized = dict(updates)
    legacy_value = normalized.pop(DAILY_SUMMARY_TIME, None)
    if legacy_value is not None and DAILY_SUMMARY_SCHEDULE not in normalized:
        normalized[DAILY_SUMMARY_SCHEDULE] = legacy_value
    parsed: dict[str, str | int | bool] = {}
    for key, value in normalized.items():
        definition = REGISTRY.get(key)
        if definition is None:
            raise SystemConfigValidationError(f"unknown system configuration key: {key}")
        parsed[key] = definition.parser(value)
    return parsed


def get_config(db: Session, key: str) -> str | int | bool:
    """Read a registered setting with legacy database values parsed at this boundary."""
    definition = REGISTRY.get(key)
    if definition is None:
        raise SystemConfigValidationError(f"unknown system configuration key: {key}")
    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if row is None or row.config_value is None:
        return definition.default
    return definition.parser(row.config_value)


def set_config(db: Session, key: str, value: Any) -> None:
    """Persist one validated runtime setting."""
    parsed = validate_updates({key: value})[key]
    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if row is None:
        db.add(SystemConfig(config_key=key, config_value=parsed))
    else:
        row.config_value = parsed
