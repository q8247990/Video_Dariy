"""Unit tests for src.services.keyframe_extractor skeleton (T1.1).

Wave 1 only covers the public surface and REPORT §7 runtime guards.
Algorithm implementation tests land in Wave 2 (T1.2-T1.9).
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from src.services.keyframe_extractor import (
    KeyframeExtractionError,
    KeyframeSet,
    extract_keyframes_for_sub_chunk,
)


def test_extract_returns_keyframe_set_shape() -> None:
    """Public signature must be stable and NotImplementedError is acceptable in Wave 1."""
    with pytest.raises(NotImplementedError):
        extract_keyframes_for_sub_chunk(["/nonexistent.mp4"])

    sig = inspect.signature(extract_keyframes_for_sub_chunk)
    params = sig.parameters
    assert "file_paths" in params
    assert params["fps_target"].default == 2
    assert params["target_n"].default == 64
    assert params["jpeg_quality"].default == 88
    assert params["mad_threshold"].default == 1.0
    assert params["phash_threshold"].default == 6
    assert params["periodic_anchor_seconds"].default == 8


def test_reports_section_seven_guards() -> None:
    """REPORT §7 runtime guards fire before NotImplementedError."""
    with pytest.raises(AssertionError):
        extract_keyframes_for_sub_chunk(["/nonexistent.mp4"], target_n=769)

    with pytest.raises(AssertionError):
        extract_keyframes_for_sub_chunk(["/nonexistent.mp4"], jpeg_quality=80)


def test_keyframe_set_is_frozen_dataclass() -> None:
    """KeyframeSet must be immutable after creation."""
    ks = KeyframeSet(
        jpeg_base64_list=[],
        fps=20.0,
        total_num_frames=0,
        frames_indices=[],
        sample_period_frames=16,
        source_duration_seconds=0.0,
    )
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        ks.fps = 10.0  # type: ignore[misc]


def test_keyframe_extraction_error_is_typed() -> None:
    """KeyframeExtractionError must be an Exception subclass with a usable message."""
    assert issubclass(KeyframeExtractionError, Exception)
    err = KeyframeExtractionError("ffmpeg failed: bad input")
    assert "bad input" in str(err)
