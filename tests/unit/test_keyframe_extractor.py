"""Unit tests for src.services.keyframe_extractor (Wave 2 / T1.2-T1.9).

Algorithm logic is exercised via monkeypatched ffmpeg/ffprobe so the
test suite does not require the ffmpeg binary.
"""

from __future__ import annotations

import dataclasses
import inspect

import numpy as np
import pytest

from src.services import keyframe_extractor as ke
from src.services.keyframe_extractor import (
    KeyframeExtractionError,
    KeyframeSet,
    extract_keyframes_for_sub_chunk,
)


def test_extract_signature_is_stable() -> None:
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
    with pytest.raises(AssertionError):
        extract_keyframes_for_sub_chunk(["/nonexistent.mp4"], target_n=769)
    with pytest.raises(AssertionError):
        extract_keyframes_for_sub_chunk(["/nonexistent.mp4"], jpeg_quality=80)


def test_keyframe_set_is_frozen_dataclass() -> None:
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
    assert issubclass(KeyframeExtractionError, Exception)
    err = KeyframeExtractionError("ffmpeg failed: bad input")
    assert "bad input" in str(err)


def test_empty_file_paths_raises() -> None:
    with pytest.raises(AssertionError, match="non-empty"):
        extract_keyframes_for_sub_chunk([])


def test_nonexistent_file_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ke, "_probe_video", lambda _p: (20.0, 10.0))
    with pytest.raises(KeyframeExtractionError, match="not found"):
        extract_keyframes_for_sub_chunk(["/tmp/does_not_exist_xyz.mp4"])


def test_algorithm_picks_change_points_then_periodic_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pathlib.Path.is_file", lambda self: True)
    base = np.zeros((90, 160, 3), dtype=np.uint8)
    frames: list[np.ndarray] = []
    for i in range(30):
        if i == 10:
            frames.append(np.full((90, 160, 3), 200, dtype=np.uint8))
        else:
            frames.append(base.copy())

    monkeypatch.setattr(ke, "_probe_video", lambda _p: (20.0, 15.0))

    def fake_decode(_path, fps_target):
        del _path, fps_target
        yield from frames

    monkeypatch.setattr(ke, "_decode_file_with_ffmpeg_pipe", fake_decode)

    ks = extract_keyframes_for_sub_chunk(
        ["/fake/a.mp4"],
        target_n=4,
        periodic_anchor_seconds=8,
    )

    assert len(ks.jpeg_base64_list) <= 4
    assert len(ks.jpeg_base64_list) % 2 == 0
    assert ks.frames_indices == sorted(ks.frames_indices)
    assert len(set(ks.frames_indices)) == len(ks.frames_indices)
    assert ks.total_num_frames == 30
    assert ks.sample_period_frames == 16
    assert ks.extra["frames_decoded"] == 30
    assert ks.extra["change_points"] >= 1
    assert ks.extra["periodic_anchors"] >= 1


def test_multi_file_indices_offset_by_prior_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pathlib.Path.is_file", lambda self: True)
    base = np.zeros((90, 160, 3), dtype=np.uint8)
    frames_a = [base.copy() for _ in range(10)]
    frames_b = [np.full((90, 160, 3), 255, dtype=np.uint8)] + [base.copy() for _ in range(9)]

    probe_calls = {"n": 0}

    def fake_probe(_p):
        probe_calls["n"] += 1
        return (20.0, 5.0)

    monkeypatch.setattr(ke, "_probe_video", fake_probe)

    def fake_decode(path, fps_target):
        del fps_target
        if str(path).endswith("a.mp4"):
            yield from frames_a
        else:
            yield from frames_b

    monkeypatch.setattr(ke, "_decode_file_with_ffmpeg_pipe", fake_decode)

    ks = extract_keyframes_for_sub_chunk(
        ["/fake/a.mp4", "/fake/b.mp4"],
        target_n=6,
        periodic_anchor_seconds=4,
    )

    assert ks.total_num_frames == 20
    if ks.frames_indices:
        assert max(ks.frames_indices) < 20
        assert ks.frames_indices == sorted(ks.frames_indices)


def test_top_n_trims_to_even_when_odd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pathlib.Path.is_file", lambda self: True)
    frames = []
    for i in range(30):
        frames.append(np.full((90, 160, 3), (i * 7) % 256, dtype=np.uint8))

    monkeypatch.setattr(ke, "_probe_video", lambda _p: (20.0, 15.0))
    monkeypatch.setattr(
        ke,
        "_decode_file_with_ffmpeg_pipe",
        lambda _p, _f: iter(frames),
    )

    ks = extract_keyframes_for_sub_chunk(["/fake/a.mp4"], target_n=5)
    assert len(ks.jpeg_base64_list) % 2 == 0
    assert len(ks.jpeg_base64_list) <= 4


def test_phash_distance_zero_for_identical_hashes() -> None:
    h = 0b1010_1010
    assert ke._phash_distance(h, h) == 0


def test_phash_distance_counts_differing_bits() -> None:
    h1 = 0x0
    h2 = 0xFFFFFFFFFFFFFFFF
    assert ke._phash_distance(h1, h2) == 64


def test_emit_jpeg_base64_produces_valid_jpeg() -> None:
    frame = np.full((480, 640, 3), 128, dtype=np.uint8)
    b64 = ke._emit_jpeg_base64(frame, jpeg_quality=90)
    import base64 as _b64
    raw = _b64.b64decode(b64)
    assert raw[:3] == b"\xff\xd8\xff"


def test_decode_pipe_raises_on_ffmpeg_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REPORT §7: ffmpeg stderr last 1KB surfaces in KeyframeExtractionError."""
    import io
    from pathlib import Path as _Path

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")  # empty stream → loop exits immediately
            self.stderr = io.BytesIO(b"x" * 2000 + b" bad_input_marker tail")

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 1

    monkeypatch.setattr(ke, "_detect_dimensions", lambda _p: (320, 240))
    monkeypatch.setattr(
        "src.services.keyframe_extractor.subprocess.Popen",
        lambda *a, **kw: _FakeProc(),
    )

    with pytest.raises(ke.KeyframeExtractionError) as exc_info:
        list(ke._decode_file_with_ffmpeg_pipe(_Path("/fake/bad.mp4"), fps_target=2))

    msg = str(exc_info.value)
    assert "bad_input_marker" in msg
    assert "exit=1" in msg


def test_decode_pipe_raises_when_ffmpeg_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FileNotFoundError from Popen surfaces as KeyframeExtractionError."""
    from pathlib import Path as _Path

    def _raise_filenotfound(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(ke, "_detect_dimensions", lambda _p: (320, 240))
    monkeypatch.setattr(
        "src.services.keyframe_extractor.subprocess.Popen", _raise_filenotfound
    )

    with pytest.raises(ke.KeyframeExtractionError, match="ffmpeg not installed"):
        list(ke._decode_file_with_ffmpeg_pipe(_Path("/fake/missing.mp4"), fps_target=2))


def test_periodic_anchor_resets_after_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pathlib.Path.is_file", lambda self: True)
    base = np.zeros((90, 160, 3), dtype=np.uint8)
    frames = []
    for i in range(20):
        if i in (5, 13):
            frames.append(np.full((90, 160, 3), 200, dtype=np.uint8))
        else:
            frames.append(base.copy())

    monkeypatch.setattr(ke, "_probe_video", lambda _p: (20.0, 10.0))
    monkeypatch.setattr(ke, "_decode_file_with_ffmpeg_pipe", lambda _p, _f: iter(frames))

    ks = extract_keyframes_for_sub_chunk(["/fake/a.mp4"], target_n=10, periodic_anchor_seconds=4)
    assert ks.extra["periodic_anchors"] >= 1
