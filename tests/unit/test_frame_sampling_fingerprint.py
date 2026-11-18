"""Tests for the frame-sampling fingerprints (src.services.frame_sampling.fingerprint).

Covers:
- ``frame_set_fingerprint``: determinism, sensitivity to frame bytes /
  timestamps / order / every policy field / the source content signature,
  and the empty-frame guard.
- ``content_signature``: a light size+head+tail content digest that never
  reads the whole file. Known limitation (by design): files with identical
  size, head and tail but different middles produce the SAME signature —
  this is the deliberate trade-off of the "light content signature".
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from src.services.frame_sampling import (
    FramePolicy,
    FrameSample,
    FrameSet,
    content_signature,
    frame_set_fingerprint,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# One valid alternative per FramePolicy field; every alternative differs from
# the built-in default so the parametrized sensitivity test is meaningful.
POLICY_ALTERNATIVES: dict[str, object] = {
    "preset": "baseline",
    "target_context_ratio": 0.8,
    "max_edge": 960,
    "probe_fps": 1.0,
    "min_frames": 8,
    "max_frames": 60,
    "jpeg_quality": 70,
    "dedupe_phash_distance": 6,
    "window_pre_seconds": 2.0,
    "window_post_seconds": 4.0,
    "max_event_windows": 2,
    "max_output_tokens": 1024,
    "enabled": False,
}

POLICY_FIELDS = sorted(POLICY_ALTERNATIVES)


def _frame(index: int, t_seconds: float) -> FrameSample:
    return FrameSample(t_seconds=t_seconds, jpeg_bytes=f"jpeg-bytes-{index}".encode())


def _frames() -> tuple[FrameSample, ...]:
    return (_frame(0, 0.5), _frame(1, 12.25), _frame(2, 30.0))


def test_frame_set_fingerprint_is_deterministic_across_calls() -> None:
    first = frame_set_fingerprint(_frames(), FramePolicy(), content_signature="sig-1")
    second = frame_set_fingerprint(_frames(), FramePolicy(), content_signature="sig-1")
    assert first == second
    assert _HEX64.fullmatch(first) is not None


def test_frame_set_fingerprint_accepts_frame_set() -> None:
    frame_set = FrameSet(frames=_frames())
    from_set = frame_set_fingerprint(frame_set, FramePolicy(), content_signature="sig-1")
    frames = list(frame_set.frames)
    from_list = frame_set_fingerprint(frames, FramePolicy(), content_signature="sig-1")
    assert from_set == from_list


def test_frame_set_fingerprint_changes_when_frame_bytes_change() -> None:
    base = frame_set_fingerprint(_frames(), FramePolicy(), content_signature="sig-1")
    frames = list(_frames())
    frames[1] = FrameSample(t_seconds=12.25, jpeg_bytes=b"different-bytes")
    assert frame_set_fingerprint(frames, FramePolicy(), content_signature="sig-1") != base


def test_frame_set_fingerprint_changes_when_timestamp_shifts_by_one_ms() -> None:
    base = frame_set_fingerprint(_frames(), FramePolicy(), content_signature="sig-1")
    frames = list(_frames())
    frames[0] = FrameSample(t_seconds=0.501, jpeg_bytes=frames[0].jpeg_bytes)
    assert frame_set_fingerprint(frames, FramePolicy(), content_signature="sig-1") != base


def test_frame_set_fingerprint_changes_when_frame_order_swaps() -> None:
    base = frame_set_fingerprint(_frames(), FramePolicy(), content_signature="sig-1")
    swapped = list(_frames())
    swapped[0], swapped[2] = swapped[2], swapped[0]
    assert frame_set_fingerprint(swapped, FramePolicy(), content_signature="sig-1") != base


@pytest.mark.parametrize("field", POLICY_FIELDS)
def test_frame_set_fingerprint_changes_when_any_single_policy_field_changes(field: str) -> None:
    base_policy = FramePolicy()
    changed_policy = FramePolicy(**{field: POLICY_ALTERNATIVES[field]})
    assert changed_policy != base_policy, "alternative value must actually differ from the default"
    base = frame_set_fingerprint(_frames(), base_policy, content_signature="sig-1")
    assert frame_set_fingerprint(_frames(), changed_policy, content_signature="sig-1") != base


def test_frame_set_fingerprint_changes_when_content_signature_changes() -> None:
    base = frame_set_fingerprint(_frames(), FramePolicy(), content_signature="sig-1")
    assert frame_set_fingerprint(_frames(), FramePolicy(), content_signature="sig-2") != base


def test_frame_set_fingerprint_rejects_empty_frame_set() -> None:
    with pytest.raises(ValueError, match="cannot fingerprint an empty frame set"):
        frame_set_fingerprint([], FramePolicy(), content_signature="sig-1")


def _flip(data: bytes, offset: int) -> bytes:
    flipped = bytearray(data)
    flipped[offset] ^= 0xFF
    return bytes(flipped)


def test_content_signature_is_64_hex_for_200kb_file(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(_random_bytes(200 * 1024))
    first = content_signature(video)
    second = content_signature(video)
    assert first == second
    assert _HEX64.fullmatch(first) is not None


def test_content_signature_changes_when_first_chunk_byte_flips(tmp_path: Path) -> None:
    data = _random_bytes(200 * 1024)
    original = tmp_path / "a.mp4"
    mutated = tmp_path / "b.mp4"
    original.write_bytes(data)
    mutated.write_bytes(_flip(data, 1024))
    assert content_signature(mutated) != content_signature(original)


def test_content_signature_unchanged_when_middle_byte_flips(tmp_path: Path) -> None:
    # Offset 100KB lies between the head (first 64KB) and the tail (last 64KB)
    # of a 200KB file, so a middle flip is invisible to the light signature.
    data = _random_bytes(200 * 1024)
    original = tmp_path / "a.mp4"
    mutated = tmp_path / "b.mp4"
    original.write_bytes(data)
    mutated.write_bytes(_flip(data, 100 * 1024))
    assert content_signature(mutated) == content_signature(original)


def test_content_signature_changes_when_last_chunk_byte_flips(tmp_path: Path) -> None:
    data = _random_bytes(200 * 1024)
    original = tmp_path / "a.mp4"
    mutated = tmp_path / "b.mp4"
    original.write_bytes(data)
    mutated.write_bytes(_flip(data, 200 * 1024 - 1))
    assert content_signature(mutated) != content_signature(original)


def test_content_signature_same_size_head_tail_different_middle(tmp_path: Path) -> None:
    # Known limitation of the light content signature, documented here on
    # purpose: it only hashes size + first chunk + last chunk. Two files with
    # identical size, head and tail but different middles therefore collide.
    # This is a deliberate trade-off (cheap, whole-file-independent digest) —
    # callers must not treat it as a full-file integrity hash.
    head = _random_bytes(64 * 1024)
    tail = _random_bytes(64 * 1024)
    file_a = tmp_path / "a.mp4"
    file_b = tmp_path / "b.mp4"
    file_a.write_bytes(head + b"\x00" * 72 * 1024 + tail)
    file_b.write_bytes(head + b"\xff" * 72 * 1024 + tail)
    assert content_signature(file_a) == content_signature(file_b)


def test_content_signature_works_for_file_smaller_than_two_chunks(tmp_path: Path) -> None:
    # 100KB < 128KB (2 * default chunk): head/tail may overlap, but the
    # digest must still be deterministic.
    video = tmp_path / "small.mp4"
    video.write_bytes(_random_bytes(100 * 1024))
    first = content_signature(video)
    second = content_signature(video)
    assert first == second
    assert _HEX64.fullmatch(first) is not None


def test_content_signature_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        content_signature(tmp_path / "does-not-exist.mp4")


def _random_bytes(size: int) -> bytes:
    return os.urandom(size)
