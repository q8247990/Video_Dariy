"""Tests for the frame-sampling value types (src.services.frame_sampling.types)."""

from __future__ import annotations

import dataclasses

import pytest

from src.services.frame_sampling import (
    FRAME_SET_SOURCES,
    FrameSample,
    FrameSampler,
    FrameSet,
    SampleRequest,
)


def test_frame_sample_construction_stores_field_values() -> None:
    frame = FrameSample(t_seconds=12.5, jpeg_bytes=b"\xff\xd8\xff", reason="motion")

    assert frame.t_seconds == 12.5
    assert frame.jpeg_bytes == b"\xff\xd8\xff"
    assert frame.reason == "motion"


def test_frame_sample_applies_default_reason() -> None:
    frame = FrameSample(t_seconds=0.0, jpeg_bytes=b"jpeg")

    assert frame.reason == "uniform"


def test_frame_sample_is_frozen() -> None:
    frame = FrameSample(t_seconds=0.0, jpeg_bytes=b"")

    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.t_seconds = 1.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.jpeg_bytes = b"mutated"
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.reason = "other"


def test_frame_sample_equality_with_identical_instances() -> None:
    assert FrameSample(t_seconds=1.0, jpeg_bytes=b"ab") == FrameSample(
        t_seconds=1.0, jpeg_bytes=b"ab"
    )


@pytest.mark.parametrize("field", ["t_seconds", "jpeg_bytes", "reason"])
def test_frame_sample_inequality_when_a_field_differs(field: str) -> None:
    base = FrameSample(t_seconds=1.0, jpeg_bytes=b"ab", reason="uniform")
    variants = {
        "t_seconds": FrameSample(t_seconds=2.0, jpeg_bytes=b"ab", reason="uniform"),
        "jpeg_bytes": FrameSample(t_seconds=1.0, jpeg_bytes=b"ac", reason="uniform"),
        "reason": FrameSample(t_seconds=1.0, jpeg_bytes=b"ab", reason="motion"),
    }

    assert base != variants[field]


def test_frame_set_construction_stores_field_values() -> None:
    frame = FrameSample(t_seconds=0.0, jpeg_bytes=b"one")
    frame_set = FrameSet(
        frames=(frame,),
        source="cache",
        elapsed_ms=42,
    )

    assert frame_set.frames == (frame,)
    assert frame_set.source == "cache"
    assert frame_set.elapsed_ms == 42


def test_frame_set_applies_default_values() -> None:
    frame_set = FrameSet(frames=(FrameSample(t_seconds=0.0, jpeg_bytes=b"x"),))

    assert frame_set.source == "sampler"
    assert frame_set.elapsed_ms == 0


def test_frame_set_is_frozen() -> None:
    frame_set = FrameSet(frames=())

    with pytest.raises(dataclasses.FrozenInstanceError):
        frame_set.frames = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame_set.source = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame_set.elapsed_ms = 1


def test_frame_set_equality_and_inequality() -> None:
    frame = FrameSample(t_seconds=0.0, jpeg_bytes=b"one")
    frame_set = FrameSet(frames=(frame,), source="sampler", elapsed_ms=5)

    assert frame_set == FrameSet(frames=(frame,), source="sampler", elapsed_ms=5)
    assert frame_set != FrameSet(frames=(frame,), source="cache", elapsed_ms=5)
    assert frame_set != FrameSet(frames=(frame,), source="sampler", elapsed_ms=6)


@pytest.mark.parametrize("source", FRAME_SET_SOURCES)
def test_frame_set_accepts_known_sources(source: str) -> None:
    frame_set = FrameSet(frames=(), source=source)

    assert frame_set.source == source


def test_frame_set_unknown_source_raises_value_error() -> None:
    with pytest.raises(ValueError):
        FrameSet(frames=(), source="mystery")


def test_sample_request_construction_stores_field_values() -> None:
    request = SampleRequest(
        video_path="/data/a.mp4",
        probe_fps=1.0,
        window_pre_seconds=1.5,
        window_post_seconds=2.5,
        min_frames=4,
        max_frames=16,
        max_edge=640,
        jpeg_quality=85,
        dedupe_phash_distance=8,
        mode="dedupe",
    )

    assert request.video_path == "/data/a.mp4"
    assert request.probe_fps == 1.0
    assert request.window_pre_seconds == 1.5
    assert request.window_post_seconds == 2.5
    assert request.min_frames == 4
    assert request.max_frames == 16
    assert request.max_edge == 640
    assert request.jpeg_quality == 85
    assert request.dedupe_phash_distance == 8
    assert request.mode == "dedupe"


def test_sample_request_applies_default_values() -> None:
    request = SampleRequest(
        video_path="/data/a.mp4",
        probe_fps=1.0,
        window_pre_seconds=1.0,
        window_post_seconds=2.0,
        min_frames=4,
        max_frames=16,
        max_edge=640,
        jpeg_quality=85,
    )

    assert request.dedupe_phash_distance is None
    assert request.mode == "uniform"


def test_sample_request_is_frozen() -> None:
    request = SampleRequest(
        video_path="/data/a.mp4",
        probe_fps=1.0,
        window_pre_seconds=1.0,
        window_post_seconds=2.0,
        min_frames=4,
        max_frames=16,
        max_edge=640,
        jpeg_quality=85,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        request.video_path = "/data/other.mp4"
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.max_frames = 32


def test_sample_request_equality_and_inequality() -> None:
    base = SampleRequest(
        video_path="/data/a.mp4",
        probe_fps=1.0,
        window_pre_seconds=1.0,
        window_post_seconds=2.0,
        min_frames=4,
        max_frames=16,
        max_edge=640,
        jpeg_quality=85,
    )

    assert base == SampleRequest(
        video_path="/data/a.mp4",
        probe_fps=1.0,
        window_pre_seconds=1.0,
        window_post_seconds=2.0,
        min_frames=4,
        max_frames=16,
        max_edge=640,
        jpeg_quality=85,
    )
    assert base != SampleRequest(
        video_path="/data/other.mp4",
        probe_fps=1.0,
        window_pre_seconds=1.0,
        window_post_seconds=2.0,
        min_frames=4,
        max_frames=16,
        max_edge=640,
        jpeg_quality=85,
    )
    assert base != SampleRequest(
        video_path="/data/a.mp4",
        probe_fps=1.0,
        window_pre_seconds=1.0,
        window_post_seconds=2.0,
        min_frames=4,
        max_frames=16,
        max_edge=640,
        jpeg_quality=85,
        mode="dedupe",
    )


def test_frame_sampler_protocol_is_structurally_satisfied() -> None:
    class _FakeSampler:
        def sample(self, request: SampleRequest) -> FrameSet:
            return FrameSet(frames=())

    assert isinstance(_FakeSampler(), FrameSampler)


def test_frame_sampler_protocol_rejects_missing_sample_method() -> None:
    class _NotASampler:
        def other(self) -> None:
            return None

    assert not isinstance(_NotASampler(), FrameSampler)
