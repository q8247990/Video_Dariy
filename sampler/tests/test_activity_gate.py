"""Tests for the sampler activity gate (static-clip frame shrink).

The gate lives in ``app.main.sample``: after extraction the probe stream is
profiled (``app.select.activity_profile``) and a mostly-static clip —
``change_ratio <= static_change_ratio`` and no ffmpeg scene score reaching
``SCENE_THRESHOLD`` — is shrunk to the single "before -> after" pair from
``select_static_pair``, bypassing ``select_frames`` and its ``min_frames``
floor. Everything else (any active clip, gate disabled) must keep byte-for-
byte the pre-gate ``select_frames`` behaviour.

No ffmpeg and no database: ``extract_frames`` is monkeypatched to return
synthetic Pillow-encoded frames, so the gate's decisions depend only on the
stream's pixels and scene scores.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np
import pytest
from app import main
from app.config import get_settings
from app.models import ExtractedFrame, ExtractionResult
from app.select import (
    ActivityProfile,
    _small_gray,
    activity_profile,
    hamming,
    phash,
    select_frames,
    select_static_pair,
)
from fastapi.testclient import TestClient
from PIL import Image

_SAMPLER_ENV_VARS = (
    "SAMPLER_HOST",
    "SAMPLER_PORT",
    "SAMPLER_ALLOWED_ROOTS",
    "SAMPLER_VERSION",
)


@pytest.fixture(autouse=True)
def clean_sampler_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a clean, deterministic environment."""
    for name in _SAMPLER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Synthetic frame builders (Pillow only, no ffmpeg)
# ---------------------------------------------------------------------------


def _jpeg(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _solid(color: tuple[int, int, int], size: int = 64) -> np.ndarray:
    array = np.zeros((size, size, 3), dtype=np.uint8)
    array[:, :] = color
    return array


def _noise(seed: int, size: int = 64) -> np.ndarray:
    rng = np.random.default_rng(seed)
    gray = rng.integers(0, 256, size=(size, size), dtype=np.uint8)
    return np.stack([gray, gray, gray], axis=-1)


def _checkerboard(size: int = 64, cell: int = 8) -> np.ndarray:
    yy, xx = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
    value = np.where(((xx // cell) + (yy // cell)) % 2 == 1, 255, 0).astype(np.uint8)
    return np.stack([value] * 3, axis=-1)


def _empty_room(count: int = 60, step: float = 0.5) -> list:
    """Fully static clip: every frame is the same solid grey room."""
    jpeg = _jpeg(_solid((120, 120, 120)))
    return [_extracted_frame(step * i, jpeg) for i in range(count)]


def _subject_appears(count: int = 80, at: int = 60, step: float = 0.5) -> list:
    """Static red wall; a checkerboard "subject" appears at frame ``at`` (t=30.0)."""
    wall = _jpeg(_solid((255, 0, 0)))
    subject = _jpeg(_checkerboard())
    return [_extracted_frame(step * i, wall if i < at else subject) for i in range(count)]


def _flickering(count: int = 12, step: float = 0.5) -> list:
    """Lighting/noise flicker: every consecutive frame pair differs strongly."""
    return [_extracted_frame(step * i, _jpeg(_noise(i % 2 + 1))) for i in range(count)]


def _all_distinct(count: int = 12, step: float = 0.5) -> list:
    """Genuinely active clip: every frame is its own fixed-seed noise pattern."""
    return [_extracted_frame(step * i, _jpeg(_noise(100 + i))) for i in range(count)]


def _extracted_frame(t_seconds: float, jpeg_bytes: bytes) -> ExtractedFrame:
    return ExtractedFrame(t_seconds=t_seconds, jpeg_bytes=jpeg_bytes)


def _install_extract(
    monkeypatch: pytest.MonkeyPatch,
    frames: list,
    *,
    scene_scores: tuple[float, ...] = (),
) -> None:
    """Replace ``main.extract_frames`` with a canned synthetic result."""

    def _fake_extract(video_path: Any, **_kwargs: Any) -> ExtractionResult:
        return ExtractionResult(
            frames=tuple(frames),
            candidates=(),
            elapsed_ms=7,
            duration_seconds=frames[-1].t_seconds + 1.0 if frames else 0.0,
            scene_scores=tuple(scene_scores),
        )

    monkeypatch.setattr(main, "extract_frames", _fake_extract)


def _post_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **overrides: object,
) -> httpx.Response:
    """POST /sample with the fake extraction installed and roots whitelisted."""
    root = tmp_path / "allowed"
    root.mkdir(exist_ok=True)
    video = root / "clip.mp4"
    video.write_bytes(b"unused; extraction is patched")
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))
    body: dict[str, object] = {"video_path": str(video)}
    body.update(overrides)
    with TestClient(main.app) as client:
        return client.post("/sample", json=body)


def _frame_times(body: dict[str, Any]) -> list[float]:
    return [frame["t_seconds"] for frame in body["frames"]]


def _frame_jpegs(body: dict[str, Any]) -> list[bytes]:
    return [base64.b64decode(frame["jpeg_base64"]) for frame in body["frames"]]


# ---------------------------------------------------------------------------
# Endpoint: the static path
# ---------------------------------------------------------------------------


def test_fully_static_clip_returns_exactly_two_near_identical_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty room: the pair is the whole LLM payload (2 < the min_frames=4 floor)."""
    frames = _empty_room()
    _install_extract(monkeypatch, frames)

    response = _post_sample(tmp_path, monkeypatch, min_frames=4, max_frames=120, mode="uniform")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"frames", "elapsed_ms"}
    assert len(body["frames"]) == 2
    jpegs = _frame_jpegs(body)
    assert hamming(phash(jpegs[0]), phash(jpegs[1])) == 0
    gray_a = np.asarray(Image.open(io.BytesIO(jpegs[0])).convert("L"), dtype=np.float64)
    gray_b = np.asarray(Image.open(io.BytesIO(jpegs[1])).convert("L"), dtype=np.float64)
    assert float(np.max(np.abs(gray_a - gray_b))) < 2.0


def test_static_pair_brackets_the_subject_appearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subject appears at t=30.0: the pair is the spike frame + its predecessor."""
    _install_extract(monkeypatch, _subject_appears())

    response = _post_sample(tmp_path, monkeypatch, min_frames=4, max_frames=120)

    assert response.status_code == 200
    times = _frame_times(response.json())
    assert len(times) == 2
    peak = [t for t in times if 29.5 <= t <= 30.5]
    assert peak, f"spike frame missing from {times}"
    assert times == [29.5, 30.0]


def test_static_budget_one_returns_the_peak_frame_not_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """static_max_frames=1 collapses the pair to the peak frame only."""
    _install_extract(monkeypatch, _subject_appears())

    response = _post_sample(
        tmp_path,
        monkeypatch,
        min_frames=4,
        max_frames=120,
        activity_gate=True,
        static_max_frames=1,
    )

    assert response.status_code == 200
    times = _frame_times(response.json())
    assert times == [30.0]


def test_scene_score_reaches_threshold_so_gate_stays_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ffmpeg scene cut marks the clip non-static even with zero pixel motion."""
    _install_extract(monkeypatch, _empty_room(), scene_scores=(0.4,))

    response = _post_sample(
        tmp_path, monkeypatch, min_frames=4, max_frames=8, mode="uniform", static_max_frames=2
    )

    assert response.status_code == 200
    assert len(response.json()["frames"]) == 8


# ---------------------------------------------------------------------------
# Endpoint: invariance of the active path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "clip", "max_frames"),
    [
        ("dedupe", _flickering, 8),
        ("uniform", _all_distinct, 6),
        ("keyframe", _all_distinct, 5),
    ],
)
def test_active_clip_output_identical_to_select_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    clip: Callable[[int], list],
    max_frames: int,
) -> None:
    """Active clips keep today's exact selection: same frames, same order, same bytes."""
    frames = clip()
    _install_extract(monkeypatch, frames)

    response = _post_sample(tmp_path, monkeypatch, min_frames=4, max_frames=max_frames, mode=mode)

    assert response.status_code == 200
    expected = select_frames(
        frames,
        [],
        budget=max_frames,
        min_frames=4,
        mode=mode,
    )
    assert _frame_times(response.json()) == [frame.t_seconds for frame in expected]
    assert _frame_jpegs(response.json()) == [frame.jpeg_bytes for frame in expected]


def test_gate_disabled_static_clip_keeps_full_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """activity_gate=False restores today's behaviour on a static clip."""
    _install_extract(monkeypatch, _empty_room())

    response = _post_sample(
        tmp_path,
        monkeypatch,
        min_frames=4,
        max_frames=8,
        mode="uniform",
        activity_gate=False,
    )

    assert response.status_code == 200
    times = _frame_times(response.json())
    assert len(times) == 8
    assert times[0] == 0.0


# ---------------------------------------------------------------------------
# Unit level: activity_profile / select_static_pair boundaries
# ---------------------------------------------------------------------------


def test_activity_profile_short_stream_is_neutral() -> None:
    assert activity_profile([], [], mad_threshold=1.0, phash_threshold=6) == ActivityProfile(
        change_ratio=0.0, max_score=0.0, peak_index=0
    )
    one = [_small_gray(_jpeg(_solid((9, 9, 9))))]
    assert activity_profile(one, [0], mad_threshold=1.0, phash_threshold=6) == ActivityProfile(
        change_ratio=0.0, max_score=0.0, peak_index=0
    )


def test_select_static_pair_at_clip_end_picks_available_neighbour() -> None:
    frames = _all_distinct(4)
    score = [0.0, 0.0, 0.0, 5.0]  # peak is the final frame

    pair = select_static_pair(frames, score, budget=2)

    assert [frame.t_seconds for frame in pair] == [1.0, 1.5]


def test_select_static_pair_budget_at_or_above_stream_returns_all() -> None:
    frames = _all_distinct(2)
    assert select_static_pair(frames, [0.0, 7.0], budget=2) == frames
