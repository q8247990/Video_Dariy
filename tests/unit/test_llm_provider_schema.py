"""Unit tests for src.schemas.llm_provider Pydantic schema.

Wave 2 / T3.4 — covers the three new field validators:
- video_preprocess_mode enum check (case-insensitive)
- video_keyframe_target_n in [16, 256]
- video_keyframe_jpeg_quality in [50, 100]
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.llm_provider import LLMProviderBase


def _base_kwargs() -> dict:
    return {
        "provider_name": "test",
        "api_base_url": "http://example.com/v1",
        "model_name": "qwen3.5-9b",
    }


def test_video_preprocess_mode_default_is_raw_mp4() -> None:
    obj = LLMProviderBase(**_base_kwargs())
    assert obj.video_preprocess_mode == "raw_mp4"


def test_video_preprocess_mode_accepts_raw_mp4() -> None:
    obj = LLMProviderBase(**_base_kwargs(), video_preprocess_mode="raw_mp4")
    assert obj.video_preprocess_mode == "raw_mp4"


def test_video_preprocess_mode_is_case_insensitive() -> None:
    obj = LLMProviderBase(**_base_kwargs(), video_preprocess_mode="Keyframe")
    assert obj.video_preprocess_mode == "keyframe"


def test_video_preprocess_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LLMProviderBase(**_base_kwargs(), video_preprocess_mode="super_frame")
    assert "video_preprocess_mode" in str(exc_info.value)


def test_video_keyframe_target_n_default_is_120() -> None:
    obj = LLMProviderBase(**_base_kwargs())
    assert obj.video_keyframe_target_n == 120


def test_video_keyframe_target_n_rejects_zero() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LLMProviderBase(**_base_kwargs(), video_keyframe_target_n=0)
    assert "video_keyframe_target_n" in str(exc_info.value)


def test_video_keyframe_target_n_rejects_too_large() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LLMProviderBase(**_base_kwargs(), video_keyframe_target_n=257)
    assert "video_keyframe_target_n" in str(exc_info.value)


def test_video_keyframe_target_n_accepts_boundary_values() -> None:
    low = LLMProviderBase(**_base_kwargs(), video_keyframe_target_n=16)
    high = LLMProviderBase(**_base_kwargs(), video_keyframe_target_n=256)
    assert low.video_keyframe_target_n == 16
    assert high.video_keyframe_target_n == 256


def test_video_keyframe_jpeg_quality_default_is_88() -> None:
    obj = LLMProviderBase(**_base_kwargs())
    assert obj.video_keyframe_jpeg_quality == 88


def test_video_keyframe_jpeg_quality_rejects_too_low() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LLMProviderBase(**_base_kwargs(), video_keyframe_jpeg_quality=49)
    assert "video_keyframe_jpeg_quality" in str(exc_info.value)


def test_video_keyframe_jpeg_quality_rejects_too_high() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LLMProviderBase(**_base_kwargs(), video_keyframe_jpeg_quality=101)
    assert "video_keyframe_jpeg_quality" in str(exc_info.value)


def test_video_keyframe_jpeg_quality_accepts_boundary_values() -> None:
    low = LLMProviderBase(**_base_kwargs(), video_keyframe_jpeg_quality=50)
    high = LLMProviderBase(**_base_kwargs(), video_keyframe_jpeg_quality=100)
    assert low.video_keyframe_jpeg_quality == 50
    assert high.video_keyframe_jpeg_quality == 100
