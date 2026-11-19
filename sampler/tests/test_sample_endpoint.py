"""Contract tests for POST /sample (todo 19).

Covers the fixed HTTP contract the backend client (todo 20) codes against:
path safety (400), schema validation (400), FrameSelectionError (400 with the
minimum in the message), undecodable video (422 with an ffmpeg hint), the
200 happy path (frame count bounds, base64 JPEGs, ascending timestamps),
event-mode scene-window selection, budget enforcement and the
max_edge / jpeg_quality re-encoding guarantee.

Fixture videos are generated with ffmpeg lavfi inside an allowed tmp root.
"""

from __future__ import annotations

import base64
import subprocess
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from app import main
from app.config import get_settings
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
# Fixture video generation
# ---------------------------------------------------------------------------


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def _make_testsrc(path: Path, size: str, duration: float = 20.0, rate: float = 10.0) -> Path:
    """Generate a ``duration``-second testsrc clip at ``size``/``rate`` (mjpeg in mp4)."""
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration:g}:rate={rate:g}:size={size}",
            "-c:v",
            "mjpeg",
            "-q:v",
            "5",
            str(path),
        ]
    )
    return path


def _make_testsrc_fast(path: Path, size: str, duration: float = 2.0, rate: float = 10.0) -> Path:
    """Generate a short libx264 ``ultrafast`` clip (fast at 4K resolutions)."""
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration:g}:rate={rate:g}:size={size}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path


def _make_scene_cut(path: Path) -> Path:
    """20s clip: 10s black then 10s white — one hard scene cut at t=10s."""
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=10:r=10",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=320x240:d=10:r=10",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-c:v",
            "mjpeg",
            "-q:v",
            "5",
            str(path),
        ]
    )
    return path


def _payload(path: Path, **overrides: object) -> dict[str, object]:
    """Full-contract request body with the documented defaults.

    ``path`` is the default ``video_path``; individual fields (including
    ``video_path``) can be overridden via keyword arguments.
    """
    body: dict[str, object] = {
        "video_path": str(path),
        "probe_fps": 2.0,
        "min_frames": 4,
        "max_frames": 120,
        "max_edge": 640,
        "jpeg_quality": 85,
        "mode": "uniform",
        "dedupe_phash_distance": None,
        "window_pre_seconds": 5.0,
        "window_post_seconds": 10.0,
        "max_event_windows": 4,
    }
    body.update(overrides)
    return body


def _post(client: TestClient, body: dict[str, object]) -> httpx.Response:
    return client.post("/sample", json=body)


# ---------------------------------------------------------------------------
# Path safety: 400
# ---------------------------------------------------------------------------


def test_sample_400_when_no_roots_configured(tmp_path: Path) -> None:
    """SAMPLER_ALLOWED_ROOTS empty: nothing is allowed, even a real file."""
    video = _make_testsrc(tmp_path / "clip.mp4", "320x240", duration=2)
    with TestClient(main.app) as client:
        response = _post(client, _payload(video))
    assert response.status_code == 400
    assert "detail" in response.json()


def test_sample_400_when_path_outside_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    _make_testsrc(root / "clip.mp4", "320x240", duration=2)
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"\x00" * 64)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(outside))
    assert response.status_code == 400
    assert "allowed roots" in response.json()["detail"]


def test_sample_400_dotdot_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        # Relative form with '..' (as sent by a careless client).
        relative_body = _payload(root / "x.mp4", video_path="../../etc/passwd")
        response = client.post("/sample", json=relative_body)
        assert response.status_code == 400
        # Absolute traversal that normalises outside the root.
        response = client.post(
            "/sample", json=_payload(root / "x.mp4", video_path=f"{root}/../outside.mp4")
        )
        assert response.status_code == 400


def test_sample_400_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    _make_testsrc(root / "clip.mp4", "320x240", duration=2)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = client.post("/sample", json=_payload(root / "clip.mp4", video_path="clip.mp4"))
    assert response.status_code == 400
    assert "absolute" in response.json()["detail"]


def test_sample_400_symlink_escaping_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"\x00" * 64)
    link = root / "sneaky.mp4"
    link.symlink_to(outside)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(link))
    assert response.status_code == 400
    assert "allowed roots" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Schema / validation errors: 400
# ---------------------------------------------------------------------------


def test_sample_400_schema_validation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    video = _make_testsrc(root / "clip.mp4", "320x240", duration=2)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        cases = [
            _payload(video, mode="bogus"),
            _payload(video, probe_fps=0.0),
            _payload(video, max_event_windows=0),
            _payload(video, min_frames=0),
            {},  # missing video_path entirely
        ]
        for body in cases:
            response = _post(client, body)
            assert response.status_code == 400, body
            assert "detail" in response.json()


# ---------------------------------------------------------------------------
# FrameSelectionError: 400 with the minimum in the message
# ---------------------------------------------------------------------------


def test_sample_400_frame_selection_error_mentions_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    video = _make_testsrc(root / "clip.mp4", "320x240", duration=2)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(video, min_frames=8, max_frames=3))
    assert response.status_code == 400
    assert "minimum" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Undecodable video: 422 with an ffmpeg hint
# ---------------------------------------------------------------------------


def test_sample_422_corrupt_video_has_ffmpeg_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    junk = root / "corrupt.mp4"
    junk.write_bytes(b"this is not a video file at all " * 32)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(junk))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "invalid data" in detail.lower() or "ffmpeg" in detail.lower()


# ---------------------------------------------------------------------------
# 200 happy path
# ---------------------------------------------------------------------------


def test_sample_200_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    video = _make_testsrc(root / "clip.mp4", "320x240", duration=20, rate=10)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(video))
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"frames", "elapsed_ms"}
    assert "candidates" not in body
    assert "duration_seconds" not in body

    frames = body["frames"]
    assert isinstance(frames, list)
    assert 1 <= len(frames) <= 120
    for frame in frames:
        assert set(frame.keys()) == {"t_seconds", "jpeg_base64"}
        jpeg = base64.b64decode(frame["jpeg_base64"])
        assert jpeg[:2] == b"\xff\xd8", "jpeg_base64 must decode to JPEG bytes"

    times = [frame["t_seconds"] for frame in frames]
    assert times == sorted(times)
    assert len(set(times)) == len(times), "t_seconds must be strictly ascending"

    assert body["elapsed_ms"] >= 0


# ---------------------------------------------------------------------------
# event mode: frames come from the scene-cut candidate window
# ---------------------------------------------------------------------------


def test_sample_event_mode_selects_frames_near_scene_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    video = _make_scene_cut(root / "cut.mp4")
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(video, mode="event", max_frames=10))
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"frames", "elapsed_ms"}

    frames = body["frames"]
    assert len(frames) == 10
    assert any(5.0 <= frame["t_seconds"] <= 20.0 for frame in frames)
    assert "candidates" not in body
    assert "duration_seconds" not in body


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_sample_max_frames_budget_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    video = _make_testsrc(root / "clip.mp4", "320x240", duration=20, rate=10)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(video, min_frames=1, max_frames=3))
    assert response.status_code == 200
    assert len(response.json()["frames"]) == 3


# ---------------------------------------------------------------------------
# Resized output: long edge <= max_edge, aspect preserved, no upscale
# ---------------------------------------------------------------------------


def test_sample_resized_output_within_max_edge_aspect_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    # 160x120 fixture: long edge == max_edge, frames must pass through
    # unchanged (never upscaled).
    small = _make_testsrc(root / "small.mp4", "160x120", duration=2, rate=10)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(small, max_edge=160))
    assert response.status_code == 200
    for frame in response.json()["frames"]:
        image = Image.open(BytesIO(base64.b64decode(frame["jpeg_base64"])))
        width, height = image.size
        assert max(width, height) <= 160
        # Aspect 4:3 preserved within 1px.
        assert abs(width - height * 4.0 / 3.0) <= 1


def test_sample_max_edge_downscales_larger_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    big = _make_testsrc(root / "big.mp4", "320x240", duration=2, rate=10)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(big, max_edge=160))
    assert response.status_code == 200
    for frame in response.json()["frames"]:
        image = Image.open(BytesIO(base64.b64decode(frame["jpeg_base64"])))
        width, height = image.size
        assert max(width, height) <= 160
        assert (width, height) == (160, 120), "320x240 at max_edge=160 must become 160x120"


# ---------------------------------------------------------------------------
# Resolution box (todo 40): out-of-box 4K rejected by default, opt-in via max_edge
# ---------------------------------------------------------------------------


def test_sample_422_out_of_box_source_without_max_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    video = _make_testsrc_fast(root / "clip4k.mp4", "3840x2160", duration=2, rate=10)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(video, max_edge=None))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "3840x2160" in detail
    assert "2560x1440" in detail
    assert "max_edge" in detail


def test_sample_422_out_of_box_source_with_large_max_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    video = _make_testsrc_fast(root / "clip4k.mp4", "3840x2160", duration=2, rate=10)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(video, max_edge=2560))
    assert response.status_code == 422
    assert "max_edge" in response.json()["detail"]


def test_sample_200_out_of_box_source_with_max_edge_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    video = _make_testsrc_fast(root / "clip4k.mp4", "3840x2160", duration=2, rate=10)
    monkeypatch.setenv("SAMPLER_ALLOWED_ROOTS", str(root))

    with TestClient(main.app) as client:
        response = _post(client, _payload(video, max_edge=1920))
    assert response.status_code == 200
    for frame in response.json()["frames"]:
        image = Image.open(BytesIO(base64.b64decode(frame["jpeg_base64"])))
        width, height = image.size
        assert (width, height) == (1920, 1080)
        assert width % 2 == 0 and height % 2 == 0
