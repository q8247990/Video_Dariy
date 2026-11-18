import pytest

from src.services.system_config_registry import (
    FRAME_SAMPLER_ENABLED,
    FRAME_SAMPLER_TIMEOUT_SECONDS,
    FRAME_SAMPLER_URL,
    FRAME_SAMPLING_SESSION_FRAME_LIMIT,
    HOME_TIMEZONE,
    REGISTRY,
    SystemConfigValidationError,
    validate_updates,
)


def test_validate_updates_rejects_unknown_key() -> None:
    with pytest.raises(SystemConfigValidationError, match="unknown"):
        validate_updates({"not_a_system_config_key": "value"})


def test_validate_updates_rejects_invalid_setting_type() -> None:
    with pytest.raises(SystemConfigValidationError, match="integer"):
        validate_updates({"scan_interval_seconds": "fast"})


def test_validate_updates_normalizes_legacy_daily_schedule_alias() -> None:
    assert validate_updates({"daily_summary_time": "08:30"}) == {"daily_summary_schedule": "08:30"}


def test_home_timezone_defaults_to_asia_shanghai() -> None:
    assert REGISTRY[HOME_TIMEZONE].default == "Asia/Shanghai"


def test_validate_updates_accepts_iana_home_timezone() -> None:
    assert validate_updates({HOME_TIMEZONE: "America/New_York"}) == {
        HOME_TIMEZONE: "America/New_York"
    }


def test_validate_updates_rejects_invalid_home_timezone() -> None:
    with pytest.raises(SystemConfigValidationError, match="IANA"):
        validate_updates({HOME_TIMEZONE: "Mars/Olympus"})


def test_frame_sampling_defaults_present_in_registry() -> None:
    assert REGISTRY[FRAME_SAMPLER_ENABLED].default is False
    assert REGISTRY[FRAME_SAMPLER_URL].default == ""
    assert REGISTRY[FRAME_SAMPLER_TIMEOUT_SECONDS].default == 120
    assert REGISTRY[FRAME_SAMPLING_SESSION_FRAME_LIMIT].default == 0


def test_validate_updates_accepts_frame_sampling_values() -> None:
    assert validate_updates(
        {
            FRAME_SAMPLER_ENABLED: True,
            FRAME_SAMPLER_URL: "http://127.0.0.1:8081",
            FRAME_SAMPLER_TIMEOUT_SECONDS: 300,
            FRAME_SAMPLING_SESSION_FRAME_LIMIT: 24,
        }
    ) == {
        "frame_sampler_enabled": True,
        "frame_sampler_url": "http://127.0.0.1:8081",
        "frame_sampler_timeout_seconds": 300,
        "frame_sampling_session_frame_limit": 24,
    }


def test_validate_updates_rejects_timeout_zero() -> None:
    with pytest.raises(SystemConfigValidationError):
        validate_updates({FRAME_SAMPLER_TIMEOUT_SECONDS: 0})


def test_validate_updates_accepts_session_frame_limit_zero() -> None:
    assert validate_updates({FRAME_SAMPLING_SESSION_FRAME_LIMIT: 0}) == {
        FRAME_SAMPLING_SESSION_FRAME_LIMIT: 0
    }


def test_validate_updates_rejects_session_frame_limit_negative() -> None:
    with pytest.raises(SystemConfigValidationError):
        validate_updates({FRAME_SAMPLING_SESSION_FRAME_LIMIT: -1})
