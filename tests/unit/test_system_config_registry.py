import pytest

from src.services.system_config_registry import (
    FRAME_SAMPLER_CONF_THRESHOLD,
    FRAME_SAMPLER_ENABLED,
    FRAME_SAMPLER_MIN_AREA_RATIO,
    FRAME_SAMPLER_MODEL_PATH,
    FRAME_SAMPLER_QUEUE_LIMIT,
    FRAME_SAMPLER_TIMEOUT_SECONDS,
    FRAME_SAMPLER_TRACK_HIGH_THRESH,
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
    assert REGISTRY[FRAME_SAMPLER_QUEUE_LIMIT].default == 8
    assert REGISTRY[FRAME_SAMPLER_MODEL_PATH].default == ""
    assert REGISTRY[FRAME_SAMPLER_CONF_THRESHOLD].default == 0.35
    assert REGISTRY[FRAME_SAMPLER_TRACK_HIGH_THRESH].default == 0.45
    assert REGISTRY[FRAME_SAMPLER_MIN_AREA_RATIO].default == 0.001
    assert REGISTRY[FRAME_SAMPLING_SESSION_FRAME_LIMIT].default == 0


def test_validate_updates_accepts_frame_sampling_values() -> None:
    assert validate_updates(
        {
            FRAME_SAMPLER_ENABLED: True,
            FRAME_SAMPLER_URL: "http://127.0.0.1:8081",
            FRAME_SAMPLER_TIMEOUT_SECONDS: 300,
            FRAME_SAMPLER_QUEUE_LIMIT: 16,
            FRAME_SAMPLER_MODEL_PATH: "/models/yolo.onnx",
            FRAME_SAMPLER_CONF_THRESHOLD: 0.5,
            FRAME_SAMPLER_TRACK_HIGH_THRESH: 0.4,
            FRAME_SAMPLER_MIN_AREA_RATIO: 0.002,
            FRAME_SAMPLING_SESSION_FRAME_LIMIT: 24,
        }
    ) == {
        "frame_sampler_enabled": True,
        "frame_sampler_url": "http://127.0.0.1:8081",
        "frame_sampler_timeout_seconds": 300,
        "frame_sampler_queue_limit": 16,
        "frame_sampler_model_path": "/models/yolo.onnx",
        "frame_sampler_conf_threshold": 0.5,
        "frame_sampler_track_high_thresh": 0.4,
        "frame_sampler_min_area_ratio": 0.002,
        "frame_sampling_session_frame_limit": 24,
    }


def test_validate_updates_rejects_conf_threshold_above_one() -> None:
    with pytest.raises(SystemConfigValidationError):
        validate_updates({FRAME_SAMPLER_CONF_THRESHOLD: 2.0})


def test_validate_updates_rejects_conf_threshold_bool() -> None:
    with pytest.raises(SystemConfigValidationError):
        validate_updates({FRAME_SAMPLER_CONF_THRESHOLD: True})


def test_validate_updates_rejects_timeout_zero() -> None:
    with pytest.raises(SystemConfigValidationError):
        validate_updates({FRAME_SAMPLER_TIMEOUT_SECONDS: 0})


def test_validate_updates_accepts_min_area_ratio() -> None:
    assert validate_updates({FRAME_SAMPLER_MIN_AREA_RATIO: 0.001}) == {
        FRAME_SAMPLER_MIN_AREA_RATIO: 0.001
    }


def test_validate_updates_rejects_min_area_ratio_zero_and_negative() -> None:
    with pytest.raises(SystemConfigValidationError):
        validate_updates({FRAME_SAMPLER_MIN_AREA_RATIO: 0.0})
    with pytest.raises(SystemConfigValidationError):
        validate_updates({FRAME_SAMPLER_MIN_AREA_RATIO: -0.01})


def test_validate_updates_accepts_session_frame_limit_zero() -> None:
    assert validate_updates({FRAME_SAMPLING_SESSION_FRAME_LIMIT: 0}) == {
        FRAME_SAMPLING_SESSION_FRAME_LIMIT: 0
    }


def test_validate_updates_rejects_session_frame_limit_negative() -> None:
    with pytest.raises(SystemConfigValidationError):
        validate_updates({FRAME_SAMPLING_SESSION_FRAME_LIMIT: -1})
