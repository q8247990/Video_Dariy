import pytest

from src.services.system_config_registry import (
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
