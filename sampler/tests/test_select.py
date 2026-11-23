"""Tests for ``app.select`` (todo 18): budget-bounded frame selection.

Frames are built synthetically as tiny real JPEGs encoded in-test with
Pillow (solid colours, gradients, checkerboards and fixed-seed noise).
No ffmpeg is involved. The local ``ExtractedFrame`` / ``CandidateWindow``
dataclasses are structural stand-ins for the todo 17 contract
(``app.extract``); ``app.select`` never imports it.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
import pytest
from app.select import (
    FrameSelectionError,
    _small_gray,
    hamming,
    phash,
    select_frames,
)
from PIL import Image


@dataclass(frozen=True)
class ExtractedFrame:
    """Structural stand-in for ``app.extract.ExtractedFrame`` (todo 17)."""

    t_seconds: float
    jpeg_bytes: bytes


@dataclass(frozen=True)
class CandidateWindow:
    """Structural stand-in for ``app.extract.CandidateWindow`` (todo 17)."""

    t_start: float
    t_end: float
    reasons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Synthetic JPEG fixtures (Pillow only, no ffmpeg)
# ---------------------------------------------------------------------------


def _jpeg(array: np.ndarray) -> bytes:
    """Deterministically encode a uint8 array as a JPEG."""
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _solid(color: tuple[int, int, int], size: int = 64) -> np.ndarray:
    array = np.zeros((size, size, 3), dtype=np.uint8)
    array[:, :] = color
    return array


def _noise(seed: int, size: int = 64) -> np.ndarray:
    """Fixed-seed grayscale noise; deterministic across runs and platforms."""
    rng = np.random.default_rng(seed)
    gray = rng.integers(0, 256, size=(size, size), dtype=np.uint8)
    return np.stack([gray, gray, gray], axis=-1)


def _gradient(size: int = 64) -> np.ndarray:
    """Horizontal 0->255 gradient (low-frequency pattern)."""
    row = (np.linspace(0, 255, size, dtype=np.float64)).astype(np.uint8)
    return np.stack([np.tile(row, (size, 1))] * 3, axis=-1)


def _checkerboard(size: int = 64, cell: int = 8) -> np.ndarray:
    """Checkerboard pattern (high-frequency pattern)."""
    yy, xx = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
    value = np.where(((xx // cell) + (yy // cell)) % 2 == 1, 255, 0).astype(np.uint8)
    return np.stack([value] * 3, axis=-1)


def make_frames(count: int = 200) -> list[ExtractedFrame]:
    """200 frames at 0.5 s spacing over 100 s (t = 0.0 .. 99.5)."""
    red_jpeg = _jpeg(_solid((255, 0, 0)))
    return [ExtractedFrame(t_seconds=0.5 * i, jpeg_bytes=red_jpeg) for i in range(count)]


def make_dedupe_frames() -> list[ExtractedFrame]:
    """A run of 20 identical frames + 5 distinct ones (t = 0.0 .. 12.0)."""
    red_jpeg = _jpeg(_solid((255, 0, 0)))
    frames = [ExtractedFrame(t_seconds=0.5 * i, jpeg_bytes=red_jpeg) for i in range(20)]
    for j in range(5):
        frames.append(ExtractedFrame(t_seconds=0.5 * (20 + j), jpeg_bytes=_jpeg(_noise(j + 1))))
    return frames


def make_identical_frames(count: int = 40) -> list[ExtractedFrame]:
    """A long run of byte-identical frames (t = 0.0 .. 0.5 * (count - 1))."""
    red_jpeg = _jpeg(_solid((255, 0, 0)))
    return [ExtractedFrame(t_seconds=0.5 * i, jpeg_bytes=red_jpeg) for i in range(count)]


def make_far_apart_frames(count: int = 20) -> list[ExtractedFrame]:
    """Frames whose pairwise pHash distances exceed the default threshold."""
    return [
        ExtractedFrame(t_seconds=0.5 * i, jpeg_bytes=_jpeg(_noise(i + 1))) for i in range(count)
    ]


def _faint_patch(base: np.ndarray) -> np.ndarray:
    """``base`` with a faint 8x8 brightening patch: sub-threshold change."""
    array = base.copy()
    patch = array[16:24, 16:24].astype(np.float64) * 0.8 + 20.0
    array[16:24, 16:24] = np.clip(patch, 0, 255).astype(np.uint8)
    return array


def _make_mixed_clip(
    change_at: dict[int, np.ndarray], count: int = 40, base: np.ndarray | None = None
) -> list[ExtractedFrame]:
    """``count`` byte-identical frames at 0.5 s spacing (t = 0.0 ..), solid red
    unless a ``base`` array is given, with the frames at the given indices
    replaced by the given arrays."""
    base_jpeg = _jpeg(_solid((255, 0, 0)) if base is None else base)
    frames = [ExtractedFrame(t_seconds=0.5 * i, jpeg_bytes=base_jpeg) for i in range(count)]
    for idx, array in change_at.items():
        frames[idx] = ExtractedFrame(t_seconds=0.5 * idx, jpeg_bytes=_jpeg(array))
    return frames


def _timestamps(frames: list[ExtractedFrame]) -> list[float]:
    return [f.t_seconds for f in frames]


# ---------------------------------------------------------------------------
# uniform mode
# ---------------------------------------------------------------------------


def test_uniform_exact_count_ascending_endpoints_and_zones() -> None:
    frames = make_frames(200)
    selected = select_frames(frames, [], budget=10, min_frames=1, mode="uniform")

    assert len(selected) == 10
    ts = _timestamps(selected)
    # Strictly ascending, unique timestamps
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))
    # First and last frame are always present
    assert ts[0] == 0.0
    assert ts[-1] == 99.5
    # Index-proportional even spacing: round(i * 199 / 9) -> t = idx * 0.5
    assert ts == [0.0, 11.0, 22.0, 33.0, 44.0, 55.5, 66.5, 77.5, 88.5, 99.5]
    # Head zone (first 5 s) and tail zone (last 10 s) are covered
    assert any(t <= 5.0 for t in ts)
    assert any(t >= 99.5 - 10.0 for t in ts)


def test_budget_equal_to_frame_count_returns_all_in_time_order() -> None:
    frames = make_frames(200)
    selected = select_frames(list(reversed(frames)), [], budget=200, min_frames=1, mode="uniform")
    assert len(selected) == 200
    assert _timestamps(selected) == [0.5 * i for i in range(200)]


def test_budget_greater_than_frame_count_returns_all_in_time_order() -> None:
    frames = make_frames(200)
    selected = select_frames(list(reversed(frames)), [], budget=500, min_frames=1, mode="uniform")
    assert len(selected) == 200
    assert _timestamps(selected) == [0.5 * i for i in range(200)]


# ---------------------------------------------------------------------------
# dedupe mode
# ---------------------------------------------------------------------------


def test_dedupe_keeps_distinct_frames_and_fills_to_exact_budget() -> None:
    frames = make_dedupe_frames()
    selected = select_frames(frames, [], budget=10, min_frames=1, mode="dedupe")
    ts = _timestamps(selected)

    # All five distinct frames are kept by the pHash walk
    assert {10.0, 10.5, 11.0, 11.5, 12.0} <= set(ts)
    # The 20 identical frames cannot starve the budget: the farthest-first
    # fill reaches exactly 10 frames while both endpoints stay selected.
    assert len(selected) == 10
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))
    assert ts[0] == 0.0
    assert ts[-1] == 12.0


def test_dedupe_threshold_changes_selection() -> None:
    frames = make_far_apart_frames(20)
    default_sel = _timestamps(select_frames(frames, [], budget=8, min_frames=1, mode="dedupe"))
    strict_sel = _timestamps(
        select_frames(frames, [], budget=8, min_frames=1, mode="dedupe", dedupe_phash_distance=64)
    )
    # The default threshold keeps every frame and even-spaces it down to the
    # budget; threshold 64 rejects the walk, so the hash-guided farthest-first
    # fill chooses instead. Both still return exactly the budget.
    assert len(default_sel) == len(strict_sel) == 8
    assert default_sel != strict_sel
    assert default_sel[0] == strict_sel[0] == 0.0
    assert default_sel[-1] == strict_sel[-1] == 9.5


def test_dedupe_nearly_identical_frames_still_return_exact_budget() -> None:
    """Replay §7.1 repro (sessions 640/753 returned 1 frame): near-identical
    content must not collapse the selection below the budget."""
    frames = make_identical_frames(40)
    selected = select_frames(frames, [], budget=8, min_frames=3, mode="dedupe")
    ts = _timestamps(selected)

    assert len(selected) == 8
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))
    # The oldest and newest frames still anchor the selection.
    assert ts[0] == 0.0
    assert ts[-1] == 19.5


def test_dedupe_all_frames_far_apart_truncates_to_exact_budget() -> None:
    """Replay §7.1 repro (sessions 648/588 returned 17 frames for budget 8):
    the pHash walk kept everything, so the budget must truncate it."""
    frames = make_far_apart_frames(20)
    selected = select_frames(frames, [], budget=8, min_frames=3, mode="dedupe")
    ts = _timestamps(selected)

    assert len(selected) == 8
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))
    assert ts[0] == 0.0
    assert ts[-1] == 9.5


def test_dedupe_max_threshold_still_returns_exact_budget_and_endpoints() -> None:
    # Threshold 64 rejects every frame after the first; the farthest-first
    # fill still has to reach the budget while keeping both endpoints.
    frames = make_far_apart_frames(20)
    selected = select_frames(
        frames, [], budget=5, min_frames=1, mode="dedupe", dedupe_phash_distance=64
    )
    ts = _timestamps(selected)

    assert len(selected) == 5
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))
    assert ts[0] == 0.0
    assert ts[-1] == 9.5


def test_dedupe_budget_above_frame_count_returns_all_frames() -> None:
    # A budget larger than the extracted stream cannot invent frames.
    frames = make_identical_frames(5)
    selected = select_frames(frames, [], budget=8, min_frames=1, mode="dedupe")
    assert _timestamps(selected) == [0.0, 0.5, 1.0, 1.5, 2.0]


# ---------------------------------------------------------------------------
# event mode
# ---------------------------------------------------------------------------


def test_event_allocates_majority_of_budget_to_window() -> None:
    frames = make_frames(200)
    selected = select_frames(
        frames, [CandidateWindow(40.0, 60.0, ("motion",))], budget=10, min_frames=1, mode="event"
    )
    ts = _timestamps(selected)

    assert len(selected) == 10
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))
    in_window = [t for t in ts if 40.0 <= t <= 60.0]
    assert len(in_window) >= 7  # 70% of the budget
    assert ts[0] == 0.0
    assert ts[-1] == 99.5


def test_event_candidate_ending_at_clip_end_covers_last_frames() -> None:
    frames = make_frames(200)
    selected = select_frames(
        frames, [CandidateWindow(95.0, 100.0, ("motion",))], budget=10, min_frames=1, mode="event"
    )
    ts = _timestamps(selected)

    # "Event only in the last frame": a frame inside the window must be picked
    assert any(t >= 95.0 for t in ts)
    # And the clip's last frame is included
    assert ts[-1] == 99.5


def test_event_without_candidates_matches_uniform() -> None:
    frames = make_frames(200)
    event_sel = _timestamps(select_frames(frames, [], budget=10, min_frames=1, mode="event"))
    uniform_sel = _timestamps(select_frames(frames, [], budget=10, min_frames=1, mode="uniform"))
    assert event_sel == uniform_sel


# ---------------------------------------------------------------------------
# keyframe mode
# ---------------------------------------------------------------------------


def test_keyframe_selects_abrupt_changes_over_static_frames() -> None:
    # Static solid-red clip with two abrupt changes: noise at t=4.0 and a
    # checkerboard at t=11.0 (both far from the 8 s / 16 s anchor grid).
    clip = _make_mixed_clip({8: _noise(101), 22: _checkerboard()})
    selected = select_frames(clip, [], budget=8, min_frames=1, mode="keyframe")
    ts = _timestamps(selected)

    assert len(ts) == 8
    assert 4.0 in ts  # the noise jump is selected
    assert 11.0 in ts  # the checkerboard jump is selected
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))
    assert ts[0] == 0.0
    assert ts[-1] == 19.5


def test_keyframe_static_clip_uses_anchors_and_even_fill() -> None:
    # All-static clip: no change points, so the 8 s / 16 s periodic anchors
    # plus the even-spacing targets make up the exact budget.
    frames = make_identical_frames(40)
    selected = select_frames(frames, [], budget=8, min_frames=1, mode="keyframe")
    ts = _timestamps(selected)

    assert ts == [0.0, 3.0, 5.5, 8.0, 8.5, 11.0, 16.0, 19.5]
    assert 8.0 in ts and 16.0 in ts  # periodic anchors present
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))


def test_keyframe_represents_two_separate_change_bursts() -> None:
    # Two 5-frame bursts of distinct noise at t=15..17 and t=23..25: the
    # duration/budget spread constraint lets at most one frame per burst
    # through, but both bursts must be represented.
    changes: dict[int, np.ndarray] = {}
    for j in range(5):
        changes[30 + j] = _noise(200 + j)
        changes[46 + j] = _noise(300 + j)
    clip = _make_mixed_clip(changes, count=80)
    selected = select_frames(clip, [], budget=8, min_frames=1, mode="keyframe")
    ts = _timestamps(selected)

    assert len(ts) == 8
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))
    assert ts[0] == 0.0
    assert ts[-1] == 39.5
    assert any(14.5 <= t <= 17.5 for t in ts), f"burst A missing: {ts}"
    assert any(22.5 <= t <= 25.5 for t in ts), f"burst B missing: {ts}"


def test_keyframe_is_deterministic_across_calls() -> None:
    clip = _make_mixed_clip({8: _noise(101), 22: _checkerboard()})
    ts_a = _timestamps(select_frames(clip, [], budget=8, min_frames=1, mode="keyframe"))
    ts_b = _timestamps(select_frames(clip, [], budget=8, min_frames=1, mode="keyframe"))
    ts_c = _timestamps(
        select_frames(list(reversed(clip)), [], budget=8, min_frames=1, mode="keyframe")
    )
    assert ts_a == ts_b == ts_c


def test_keyframe_budget_one_returns_first_frame_only() -> None:
    clip = _make_mixed_clip({8: _noise(101), 22: _checkerboard()})
    single = select_frames(clip, [], budget=1, min_frames=1, mode="keyframe")
    assert _timestamps(single) == [0.0]


def test_keyframe_budget_above_frame_count_returns_all() -> None:
    clip = _make_mixed_clip({8: _noise(101)}, count=12)
    selected = select_frames(clip, [], budget=20, min_frames=1, mode="keyframe")
    assert _timestamps(selected) == [0.5 * i for i in range(12)]


def test_keyframe_threshold_skips_low_difference_frame() -> None:
    # Static fixed-seed noise clip (rich, stable pHash spectrum) with a faint
    # patch at t=5.0 under both change thresholds and a checkerboard at
    # t=12.0 as a clear change point.
    base = _noise(42)
    clip = _make_mixed_clip({10: _faint_patch(base), 24: _checkerboard()}, count=40, base=base)
    weak_mad = float(
        np.mean(
            np.abs(
                _small_gray(clip[10].jpeg_bytes).astype(np.float64)
                - _small_gray(clip[9].jpeg_bytes).astype(np.float64)
            )
        )
    )
    weak_phd = hamming(phash(clip[10].jpeg_bytes), phash(clip[9].jpeg_bytes))
    assert weak_mad < 1.0, "fixture must stay under the default MAD threshold"
    assert weak_phd <= 6, "fixture must stay under the default pHash threshold"

    default_ts = _timestamps(select_frames(clip, [], budget=8, min_frames=1, mode="keyframe"))
    assert 12.0 in default_ts
    assert 5.0 not in default_ts

    loose_ts = _timestamps(
        select_frames(
            clip,
            [],
            budget=8,
            min_frames=1,
            mode="keyframe",
            dedupe_phash_distance=0,
            mad_threshold=0.0,
        )
    )
    assert 12.0 in loose_ts
    assert 5.0 in loose_ts  # relaxed thresholds turn the faint frame into a change point


def test_keyframe_zero_period_still_returns_exact_budget() -> None:
    # periodic_anchor_seconds <= 0 disables anchors; on an all-static clip the
    # budget then comes purely from endpoints plus even-spacing fill.
    frames = make_identical_frames(40)
    selected = select_frames(
        frames, [], budget=8, min_frames=1, mode="keyframe", periodic_anchor_seconds=0.0
    )
    ts = _timestamps(selected)

    assert ts == [0.0, 3.0, 5.5, 8.5, 11.0, 14.0, 16.5, 19.5]
    assert len(ts) == 8


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_budget_below_min_frames_raises_frame_selection_error() -> None:
    frames = make_frames(10)
    with pytest.raises(FrameSelectionError) as excinfo:
        select_frames(frames, [], budget=2, min_frames=5, mode="uniform")
    # The message must mention the minimum (5)
    assert "5" in str(excinfo.value)


def test_budget_zero_below_floor_raises_frame_selection_error() -> None:
    frames = make_frames(10)
    with pytest.raises(FrameSelectionError) as excinfo:
        select_frames(frames, [], budget=0, min_frames=0, mode="uniform")
    assert "min_frames" in str(excinfo.value)


def test_invalid_mode_raises_value_error() -> None:
    frames = make_frames(10)
    with pytest.raises(ValueError):
        select_frames(frames, [], budget=3, min_frames=1, mode="random")


# ---------------------------------------------------------------------------
# phash / hamming
# ---------------------------------------------------------------------------


def test_phash_identical_images_have_zero_distance() -> None:
    # Same content, independently re-encoded
    a = phash(_jpeg(_noise(1)))
    b = phash(_jpeg(_noise(1)))
    assert a == b
    assert hamming(a, b) == 0
    # Same bytes
    jpeg = _jpeg(_solid((0, 128, 255)))
    assert hamming(phash(jpeg), phash(jpeg)) == 0


def test_phash_very_different_patterns_have_large_distance() -> None:
    # Inverted noise vs itself inverted
    noise = _noise(2)
    inverted = (255 - noise).astype(np.uint8)
    assert hamming(phash(_jpeg(noise)), phash(_jpeg(inverted))) > 16
    # Very different patterns: low-frequency gradient vs high-frequency checker
    assert hamming(phash(_jpeg(_gradient())), phash(_jpeg(_checkerboard()))) > 16
    # Different noise seeds
    assert hamming(phash(_jpeg(_noise(1))), phash(_jpeg(_noise(2)))) > 16


def test_phash_fits_in_64_bits() -> None:
    for jpeg in (
        _jpeg(_solid((255, 0, 0))),
        _jpeg(_solid((0, 0, 0))),
        _jpeg(_noise(3)),
        _jpeg(_gradient()),
        _jpeg(_checkerboard()),
    ):
        h = phash(jpeg)
        assert isinstance(h, int)
        assert 0 <= h < 2**64
