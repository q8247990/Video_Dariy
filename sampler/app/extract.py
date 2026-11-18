"""Two-pass ffmpeg frame extraction with scene gating (todo 17).

Given a video file, ``extract_frames`` decodes it at a fixed probe rate and
returns every sampled frame as a JPEG plus a list of *event candidate
windows* — time ranges that likely contain an event, derived from ffmpeg's
scene-change scores.

The extraction runs two ffmpeg passes plus a best-effort ffprobe duration
read:

1. **Scene pass** — ``fps=<probe_fps>,select='gte(scene,0)',metadata=print``
   into a null sink. ``select`` with a ``scene`` expression makes ffmpeg
   attach a ``lavfi.scene_score`` metadata value (0 = identical to previous
   frame, 1 = completely different) to every sampled frame;
   ``metadata=print`` dumps it to stderr. This pass runs at ``-loglevel
   info`` because the metadata filter logs at AV_LOG_INFO — at
   ``-loglevel error`` nothing is printed at all.
2. **JPEG pass** — ``fps=<probe_fps> -q:v <qv> -f image2`` into a
   temporary directory, read back sorted by name. Runs at
   ``-loglevel error`` for a clean stderr.

Frame times are derived from the filter output index (``index / probe_fps``),
never from ``pts_time``: the ``fps`` filter guarantees output frames land
exactly on ``n / probe_fps``, and the ``metadata=print`` block omits
``pts_time`` for some streams. The same rule gives the JPEG pass its
timestamps (file index / probe_fps).

Candidate windows: a sampled frame with scene score >= ``SCENE_THRESHOLD``
starts a window; triggers closer together than ``MERGE_GAP_SECONDS`` merge
into one window; each window is expanded by ``window_pre_seconds`` /
``window_post_seconds`` and clamped to ``[0, duration]``. When more windows
exist than ``max_event_windows``, only the ones with the highest peak scene
score survive (ties broken by earliest start, deterministically).

A zero-frame (empty) video yields an empty :class:`app.models.ExtractionResult`
instead of an error; unreadable files, decode failures and timeouts raise
:class:`ExtractionFailed` carrying the tail of ffmpeg's stderr.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import tempfile
import time
from pathlib import Path

from app.models import CandidateWindow, ExtractedFrame, ExtractionResult

logger = logging.getLogger(__name__)

#: Scene score at or above which a sampled frame starts a candidate window.
SCENE_THRESHOLD = 0.3

#: Two triggers closer together than this (seconds) merge into one window.
MERGE_GAP_SECONDS = 1.0

#: Length of the ffmpeg stderr tail embedded in error messages.
STDERR_TAIL_CHARS = 2000

# ``metadata=print`` emits one block per frame: a header line prefixed with
# ``[Parsed_metadata_N @ 0x...] frame:<n>    pts:<pts>       pts_time:<t>``
# followed by one ``key=value`` line per metadata entry.
_FRAME_HEADER_RE = re.compile(r"frame:(\d+)\s+pts:")
_SCORE_RE = re.compile(r"lavfi\.scene_score=(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)")

#: ffmpeg muxer error identifying a stream that decodes to zero frames
#: (e.g. a valid container with an empty video track).
_NO_STREAM_MARKER = "does not contain any stream"


class ExtractionFailed(RuntimeError):
    """Frame extraction failed (unreadable file, decode error or timeout).

    The message carries the tail (~2000 chars) of ffmpeg's stderr whenever
    the failure was caused by an ffmpeg/ffprobe invocation.
    """


def jpeg_quality_to_qv(jpeg_quality: int) -> int:
    """Map a perceptual JPEG quality (60..95) to an ffmpeg mjpeg ``-q:v``.

    ffmpeg's mjpeg encoder uses ``-q:v`` 2..31 where *lower* is better, i.e.
    the opposite direction of the 0..100 JPEG quality scale. The mapping is
    a linear inverse: quality 60 -> 31 (worst), quality 95 -> 2 (best), with
    intermediate values scaled proportionally (``31 - (q - 60) * 29 / 35``,
    rounded). The result is clamped into ``[2, 31]``, so out-of-range
    quality values saturate at the boundary quality.
    """
    qv = 31 - (jpeg_quality - 60) * (31 - 2) / (95 - 60)
    return min(31, max(2, round(qv)))


def _stderr_tail(stderr: bytes | str | None) -> str:
    """Return the last ``STDERR_TAIL_CHARS`` of decoded stderr (empty-safe)."""
    if not stderr:
        return ""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    text = text.strip()
    return text[-STDERR_TAIL_CHARS:]


def _run(cmd: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[bytes]:
    """Run an ffmpeg/ffprobe subprocess with a hard timeout.

    Raises :class:`ExtractionFailed` on timeout (with any partial stderr) or
    when the binary cannot be started.
    """
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise ExtractionFailed(
            f"ffmpeg timed out after {timeout_seconds}s: {_stderr_tail(exc.stderr)}"
        ) from exc
    except OSError as exc:
        raise ExtractionFailed(f"failed to run {cmd[0]}: {exc}") from exc


def _scene_pass(
    video_path: Path, probe_fps: float, timeout_seconds: float
) -> list[tuple[float, float]]:
    """Run the scene-score pass; return ``(t_seconds, score)`` per frame.

    Returns an empty list when the stream decodes to zero frames (empty
    video) instead of raising.
    """
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(video_path),
        "-vf",
        f"fps={probe_fps:g},select='gte(scene,0)',metadata=print",
        "-f",
        "null",
        "-",
    ]
    proc = _run(cmd, timeout_seconds)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        if _NO_STREAM_MARKER in stderr:
            logger.info("No decodable frames in %s", video_path)
            return []
        raise ExtractionFailed(
            f"scene-score pass failed for {video_path}: {_stderr_tail(proc.stderr)}"
        )
    scores: list[tuple[float, float]] = []
    current_index: int | None = None
    for line in proc.stderr.decode("utf-8", "replace").splitlines():
        header = _FRAME_HEADER_RE.search(line)
        if header:
            current_index = int(header.group(1))
            continue
        score = _SCORE_RE.search(line)
        if score and current_index is not None:
            # Frame time = output index / probe_fps (see module docstring):
            # the fps filter guarantees exact ``n / probe_fps`` timestamps,
            # while pts_time may be absent from the printed block.
            scores.append((current_index / probe_fps, float(score.group(1))))
    return scores


def _jpeg_pass(
    video_path: Path, probe_fps: float, qv: int, timeout_seconds: float, tmp_dir: Path
) -> list[ExtractedFrame]:
    """Run the JPEG extraction pass; read back the frames sorted by name."""
    pattern = tmp_dir / "frame-%06d.jpg"
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vf",
        f"fps={probe_fps:g}",
        "-q:v",
        str(qv),
        "-f",
        "image2",
        str(pattern),
    ]
    proc = _run(cmd, timeout_seconds)
    if proc.returncode != 0:
        raise ExtractionFailed(
            f"jpeg extraction pass failed for {video_path}: {_stderr_tail(proc.stderr)}"
        )
    frames: list[ExtractedFrame] = []
    for index, path in enumerate(sorted(tmp_dir.glob("frame-*.jpg"))):
        frames.append(ExtractedFrame(t_seconds=index / probe_fps, jpeg_bytes=path.read_bytes()))
    return frames


def _probe_duration(video_path: Path, timeout_seconds: float) -> float:
    """Best-effort container duration via ffprobe; 0.0 when unknown/unreadable."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    try:
        value = float(proc.stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _build_candidates(
    scene_scores: list[tuple[float, float]],
    duration_seconds: float,
    window_pre_seconds: float,
    window_post_seconds: float,
    max_event_windows: int,
) -> tuple[CandidateWindow, ...]:
    """Turn scene scores into ordered, capped candidate windows."""
    triggers = [(t, s) for t, s in scene_scores if s >= SCENE_THRESHOLD]
    # Group consecutive triggers whose gap is <= MERGE_GAP_SECONDS.
    groups: list[list[float]] = []  # [first_t, last_t, peak_score]
    for t, score in triggers:
        if groups and t - groups[-1][1] <= MERGE_GAP_SECONDS:
            groups[-1][1] = t
            groups[-1][2] = max(groups[-1][2], score)
        else:
            groups.append([t, t, score])

    windows: list[tuple[float, float, float, tuple[str, ...]]] = []
    for first_t, last_t, peak in groups:
        t_start = max(0.0, first_t - window_pre_seconds)
        t_end = min(duration_seconds, last_t + window_post_seconds)
        if t_end <= t_start:
            logger.debug("Skipping degenerate candidate window [%.3f, %.3f]", t_start, t_end)
            continue
        reason: tuple[str, ...] = ("merged",) if last_t > first_t else ("scene",)
        windows.append((t_start, t_end, peak, reason))

    if len(windows) > max_event_windows:
        # Keep the highest-peak windows; ties broken by earliest start so the
        # selection is deterministic.
        windows.sort(key=lambda w: (-w[2], w[0]))
        windows = windows[:max_event_windows]
    windows.sort(key=lambda w: w[0])
    return tuple(
        CandidateWindow(t_start=start, t_end=end, reasons=reason)
        for start, end, _peak, reason in windows
    )


def extract_frames(
    video_path: str | Path,
    *,
    probe_fps: float,
    window_pre_seconds: float,
    window_post_seconds: float,
    max_event_windows: int,
    jpeg_quality: int,
    timeout_seconds: float = 120.0,
) -> ExtractionResult:
    """Extract probe-rate JPEG frames and scene-gated candidate windows.

    Args:
        video_path: Path to the video file to decode.
        probe_fps: Frames per second to sample (must be > 0).
        window_pre_seconds: Seconds of context before a scene trigger.
        window_post_seconds: Seconds of context after a scene trigger.
        max_event_windows: Cap on returned windows (>= 1); the highest-peak
            windows are kept.
        jpeg_quality: Target JPEG quality, 60..95 (mapped to ffmpeg ``-q:v``).
        timeout_seconds: Per-ffmpeg-invocation timeout in seconds.

    Returns:
        An :class:`app.models.ExtractionResult`; frames and candidates are
        both empty for a zero-frame video.

    Raises:
        ValueError: on non-positive/negative parameter values.
        ExtractionFailed: on missing file, unreadable/corrupt video, decode
            failure or timeout (message carries ffmpeg's stderr tail).
    """
    if not (isinstance(probe_fps, (int, float)) and math.isfinite(probe_fps) and probe_fps > 0):
        raise ValueError(f"probe_fps must be a finite positive number, got {probe_fps!r}")
    for name, value in (
        ("window_pre_seconds", window_pre_seconds),
        ("window_post_seconds", window_post_seconds),
    ):
        if not (isinstance(value, (int, float)) and math.isfinite(value) and value >= 0):
            raise ValueError(f"{name} must be a finite non-negative number, got {value!r}")
    if (
        not isinstance(max_event_windows, int)
        or isinstance(max_event_windows, bool)
        or max_event_windows < 1
    ):
        raise ValueError(f"max_event_windows must be an integer >= 1, got {max_event_windows!r}")

    path = Path(video_path)
    if not path.is_file():
        raise ExtractionFailed(f"video file not found: {path}")

    qv = jpeg_quality_to_qv(jpeg_quality)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="video-dairy-sampler-") as tmp:
        tmp_dir = Path(tmp)
        logger.info("Extracting frames from %s (probe_fps=%g, qv=%d)", path, probe_fps, qv)
        scene_scores = _scene_pass(path, probe_fps, timeout_seconds)
        if not scene_scores:
            # Zero decodable frames: empty result, not an error.
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return ExtractionResult(
                frames=(), candidates=(), elapsed_ms=elapsed_ms, duration_seconds=0.0
            )
        frames = _jpeg_pass(path, probe_fps, qv, timeout_seconds, tmp_dir)
        duration = _probe_duration(path, timeout_seconds)
        if duration <= 0.0 and frames:
            # Fallback: last sampled frame plus one frame period.
            duration = frames[-1].t_seconds + 1.0 / probe_fps
        candidates = _build_candidates(
            scene_scores, duration, window_pre_seconds, window_post_seconds, max_event_windows
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "Extracted %d frames and %d candidate windows from %s in %d ms",
            len(frames),
            len(candidates),
            path,
            elapsed_ms,
        )
    return ExtractionResult(
        frames=tuple(frames),
        candidates=candidates,
        elapsed_ms=elapsed_ms,
        duration_seconds=duration,
    )
