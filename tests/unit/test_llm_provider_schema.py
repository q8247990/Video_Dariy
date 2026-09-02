"""Unit tests for src.schemas.llm_provider Pydantic schema.

Covers the contract-migration safety net: legacy payloads that still
carry ``video_preprocess_mode`` / ``video_keyframe_target_n`` /
``video_keyframe_jpeg_quality`` (retired by tod 10 / migration
``20260902_0018``) must be silently ignored — neither rejected (which
would break existing clients with no API change) nor accepted in a way
that could ever re-enable the keyframe path.
"""

from __future__ import annotations

import pytest

from src.schemas.llm_provider import LLMProviderBase


def _base_kwargs() -> dict:
    return {
        "provider_name": "test",
        "api_base_url": "http://example.com/v1",
        "model_name": "qwen3.5-9b",
    }


def test_base_schema_omits_retired_keyframe_fields() -> None:
    """The retired fields are not declared on the schema anymore."""

    obj = LLMProviderBase(**_base_kwargs())
    dumped = obj.model_dump()
    assert "video_preprocess_mode" not in dumped
    assert "video_keyframe_target_n" not in dumped
    assert "video_keyframe_jpeg_quality" not in dumped


def test_legacy_video_preprocess_mode_is_silently_ignored() -> None:
    """Old clients sending ``video_preprocess_mode='keyframe'`` must not
    get a 400/422 after the contract migration; the value is dropped
    before it can activate the disabled capability."""

    obj = LLMProviderBase(
        **_base_kwargs(),
        video_preprocess_mode="keyframe",
    )
    dumped = obj.model_dump()
    assert "video_preprocess_mode" not in dumped


def test_legacy_keyframe_target_n_is_silently_ignored() -> None:
    obj = LLMProviderBase(
        **_base_kwargs(),
        video_keyframe_target_n=128,
    )
    assert "video_keyframe_target_n" not in obj.model_dump()


def test_legacy_keyframe_jpeg_quality_is_silently_ignored() -> None:
    obj = LLMProviderBase(
        **_base_kwargs(),
        video_keyframe_jpeg_quality=95,
    )
    assert "video_keyframe_jpeg_quality" not in obj.model_dump()


def test_legacy_payload_with_all_three_fields_is_silently_ignored() -> None:
    """The realistic case: a single legacy payload carries all three
    fields at once. None of them must leak into the validated payload."""

    obj = LLMProviderBase(
        **_base_kwargs(),
        video_preprocess_mode="keyframe",
        video_keyframe_target_n=128,
        video_keyframe_jpeg_quality=95,
    )
    dumped = obj.model_dump()
    assert set(dumped).isdisjoint(
        {
            "video_preprocess_mode",
            "video_keyframe_target_n",
            "video_keyframe_jpeg_quality",
        }
    )


@pytest.mark.parametrize(
    "extra_payload",
    [
        {"video_preprocess_mode": "Keyframe"},
        {"video_preprocess_mode": "RAW_MP4"},
        {"video_preprocess_mode": "super_frame"},
        {"video_keyframe_target_n": 0},
        {"video_keyframe_target_n": 257},
        {"video_keyframe_jpeg_quality": 49},
        {"video_keyframe_jpeg_quality": 101},
        {"video_keyframe_target_n": "not-an-int"},
    ],
)
def test_no_legacy_value_ever_raises(extra_payload: dict) -> None:
    """The schema must never reject any legacy value: the contract is
    'silently ignore', not 'validate against the old ruleset'."""

    obj = LLMProviderBase(**_base_kwargs(), **extra_payload)
    assert "video_preprocess_mode" not in obj.model_dump()
    assert "video_keyframe_target_n" not in obj.model_dump()
    assert "video_keyframe_jpeg_quality" not in obj.model_dump()
