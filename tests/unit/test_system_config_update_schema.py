"""PUT /system-config 请求模型（SystemConfigUpdate）对帧采样配置键的契约测试。

端点契约：``validate_updates(data.model_dump(exclude_unset=True))`` 必须可被
帧采样相关的 9 个配置键满足，且未知字段仍被 pydantic 的 extra="forbid" 拒绝。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.system_config import SystemConfigUpdate
from src.services.system_config_registry import SystemConfigValidationError, validate_updates


def test_system_config_update_accepts_frame_sampler_keys() -> None:
    payload = SystemConfigUpdate(
        frame_sampler_enabled=True,
        frame_sampler_url="http://127.0.0.1:18080",
        frame_sampler_conf_threshold=0.5,
        frame_sampler_min_area_ratio=0.002,
        frame_sampling_session_frame_limit=24,
    )
    updates = validate_updates(payload.model_dump(exclude_unset=True))
    assert updates == {
        "frame_sampler_enabled": True,
        "frame_sampler_url": "http://127.0.0.1:18080",
        "frame_sampler_conf_threshold": 0.5,
        "frame_sampler_min_area_ratio": 0.002,
        "frame_sampling_session_frame_limit": 24,
    }


def test_system_config_update_dump_covers_all_frame_sampling_keys() -> None:
    payload = SystemConfigUpdate(
        frame_sampler_enabled=True,
        frame_sampler_url="http://127.0.0.1:18080",
        frame_sampler_timeout_seconds=300,
        frame_sampler_queue_limit=16,
        frame_sampler_model_path="/models/yolo.onnx",
        frame_sampler_conf_threshold=0.5,
        frame_sampler_track_high_thresh=0.4,
        frame_sampler_min_area_ratio=0.002,
        frame_sampling_session_frame_limit=24,
    )
    updates = validate_updates(payload.model_dump(exclude_unset=True))
    assert updates == {
        "frame_sampler_enabled": True,
        "frame_sampler_url": "http://127.0.0.1:18080",
        "frame_sampler_timeout_seconds": 300,
        "frame_sampler_queue_limit": 16,
        "frame_sampler_model_path": "/models/yolo.onnx",
        "frame_sampler_conf_threshold": 0.5,
        "frame_sampler_track_high_thresh": 0.4,
        "frame_sampler_min_area_ratio": 0.002,
        "frame_sampling_session_frame_limit": 24,
    }


def test_system_config_update_rejects_conf_threshold_above_one() -> None:
    payload = SystemConfigUpdate(frame_sampler_conf_threshold=2.0)
    with pytest.raises(SystemConfigValidationError):
        validate_updates(payload.model_dump(exclude_unset=True))


def test_system_config_update_rejects_negative_session_frame_limit() -> None:
    payload = SystemConfigUpdate(frame_sampling_session_frame_limit=-1)
    with pytest.raises(SystemConfigValidationError):
        validate_updates(payload.model_dump(exclude_unset=True))


def test_system_config_update_still_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SystemConfigUpdate(not_a_key=1)  # type: ignore[call-arg]
