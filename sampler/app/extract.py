"""Single-pass ffmpeg frame extraction with scene gating (todo 17, todo 38, 40).

Given a video file, ``extract_frames`` decodes it at a fixed probe rate and
returns every sampled frame as a JPEG plus a list of *event candidate
windows* — time ranges that likely contain an event, derived from ffmpeg's
scene-change scores.

The extraction is a **single ffmpeg pass** (todo 38: one decode instead of
two). One invocation decodes the video at ``probe_fps`` with the filter chain
``fps=<probe_fps>,select='gte(scene,0)',metadata=print[,scale=...]`` and
writes the frames as JPEGs (``-f image2``). ``select`` with a ``scene``
expression makes ffmpeg attach a ``lavfi.scene_score`` metadata value (0 =
identical to previous frame, 1 = completely different) to every sampled frame;
``metadata=print`` dumps it to stderr. The pass runs at ``-loglevel info``
because the metadata filter logs at AV_LOG_INFO — at ``-loglevel error``
nothing is printed at all. The scene scores are parsed from that same stderr
in the same call that writes the JPEGs, so the decode happens exactly once.

**Resolution box** (todo 40): the supported source box is
``SUPPORTED_MAX_WIDTH x SUPPORTED_MAX_HEIGHT`` (1440p). A source that fits
the box passes through without scaling (the always-present ``scale`` filter
only aligns odd dimensions to even). A source outside the box is rejected
with :class:`ExtractionFailed` unless the caller opts into downscaling by
passing a ``max_edge`` smaller than the box long edge; then the frames are
scaled in ffmpeg to fit the ``max_edge`` square box (aspect preserved, never
upscaled, even dimensions), so the returned JPEGs are already final-size and
need no re-encoding downstream.

The source dimensions and container duration come from **one** ffprobe
invocation (``-show_entries stream=width,height:format=duration``), run
before the decode so an out-of-box source fails fast without being decoded.

Frame times are derived from the filter output index (``index / probe_fps``),
never from ``pts_time``: the ``fps`` filter guarantees output frames land
exactly on ``n / probe_fps``, and the ``metadata=print`` block omits
``pts_time`` for some streams. The same rule gives the JPEG files their
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

import json
import logging
import math
import re
import subprocess
import tempfile
import time
from pathlib import Path

from app.config import SUPPORTED_MAX_HEIGHT, SUPPORTED_MAX_WIDTH
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


def _scale_filter(box_width: int, box_height: int) -> str:
    """Build an ffmpeg ``scale`` filter fitting the frame inside ``box_width x box_height``.

    ``min(<W>,iw)`` / ``min(<H>,ih)`` cap each output dimension at the source
    dimension, so the frame is never enlarged;
    ``force_original_aspect_ratio=decrease`` preserves the aspect ratio inside
    the box; ``force_divisible_by=2`` rounds each dimension down to an even
    value (odd source dimensions become even).
    """
    return (
        f"scale='min({box_width},iw)':'min({box_height},ih)'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def _out_of_box_error(source_width: int, source_height: int) -> str:
    """Message for a source that does not fit the supported box without an explicit downscale."""
    return (
        f"source resolution {source_width}x{source_height} exceeds the supported "
        f"{SUPPORTED_MAX_WIDTH}x{SUPPORTED_MAX_HEIGHT} box: "
        f"set max_edge to a smaller value to opt into downscaling"
    )


def _resolution_box(
    source_width: int | None,
    source_height: int | None,
    max_edge: int | None,
) -> tuple[int, int] | None:
    """Resolve the ffmpeg scale box for the extraction pass (todo 40).

    Returns the ``(width, height)`` box the ``scale`` filter must fit the
    frames inside, or ``None`` for an uncheckable pass-through (source
    dimensions unknown — nothing to check, nothing to scale).

    - ``max_edge`` smaller than the box long edge is an explicit downscale
      opt-in: frames fit the ``max_edge`` square box.
    - Otherwise the source must fit the supported box; a source outside it
      raises :class:`ExtractionFailed` (fail fast, before decoding).
    - A fitting source maps to the box itself: ``min(<W>,iw)`` /
      ``min(<H>,ih)`` then equals the source size, so only odd dimensions are
      aligned to even.
    """
    if max_edge is not None and max_edge < SUPPORTED_MAX_WIDTH:
        return (max_edge, max_edge)
    if source_width is not None and source_height is not None:
        if source_width > SUPPORTED_MAX_WIDTH or source_height > SUPPORTED_MAX_HEIGHT:
            raise ExtractionFailed(_out_of_box_error(source_width, source_height))
        return (SUPPORTED_MAX_WIDTH, SUPPORTED_MAX_HEIGHT)
    return None


def _parse_scores(stderr: bytes, probe_fps: float) -> list[tuple[float, float]]:
    """Parse ``(t_seconds, scene_score)`` pairs from ``metadata=print`` stderr.

    Frame time = output index / probe_fps (see module docstring): the ``fps``
    filter guarantees exact ``n / probe_fps`` timestamps, while ``pts_time``
    may be absent from the printed block.
    """
    scores: list[tuple[float, float]] = []
    current_index: int | None = None
    for line in stderr.decode("utf-8", "replace").splitlines():
        header = _FRAME_HEADER_RE.search(line)
        if header:
            current_index = int(header.group(1))
            continue
        score = _SCORE_RE.search(line)
        if score and current_index is not None:
            scores.append((current_index / probe_fps, float(score.group(1))))
    return scores


def _extract_pass(
    video_path: Path,
    probe_fps: float,
    qv: int,
    scale_box: tuple[int, int] | None,
    timeout_seconds: float,
    tmp_dir: Path,
) -> tuple[list[tuple[float, float]], list[ExtractedFrame]]:
    """Run the single-pass ffmpeg extraction.

    One ffmpeg invocation decodes the video at ``probe_fps``, attaches a
    ``lavfi.scene_score`` to every sampled frame (``metadata=print`` to
    stderr), scales the frames into the ``scale_box`` when a box is given,
    and writes them as JPEGs. Returns ``(scene_scores, frames)``; both are
    empty when the stream decodes to zero frames.
    """
    video_filter = f"fps={probe_fps:g},select='gte(scene,0)',metadata=print"
    if scale_box is not None:
        video_filter += f",{_scale_filter(*scale_box)}"
    pattern = tmp_dir / "frame-%06d.jpg"
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(video_path),
        "-vf",
        video_filter,
        "-q:v",
        str(qv),
        "-f",
        "image2",
        str(pattern),
    ]
    proc = _run(cmd, timeout_seconds)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        if _NO_STREAM_MARKER in stderr:
            logger.info("No decodable frames in %s", video_path)
            return [], []
        raise ExtractionFailed(
            f"frame extraction failed for {video_path}: {_stderr_tail(proc.stderr)}"
        )
    scores = _parse_scores(proc.stderr, probe_fps)
    frames: list[ExtractedFrame] = []
    for index, path in enumerate(sorted(tmp_dir.glob("frame-*.jpg"))):
        frames.append(ExtractedFrame(t_seconds=index / probe_fps, jpeg_bytes=path.read_bytes()))
    return scores, frames


def _first_video_stream(streams: object) -> tuple[int, int] | None:
    """Return ``(width, height)`` of the first stream carrying both dimensions."""
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        width = stream.get("width")
        height = stream.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
    return None


def _format_duration(fmt: object) -> float:
    """Best-effort container duration in seconds; 0.0 when unknown/invalid."""
    if not isinstance(fmt, dict):
        return 0.0
    try:
        value = float(fmt.get("duration", "nan"))
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _probe_video_info(
    video_path: Path, timeout_seconds: float
) -> tuple[float, int | None, int | None]:
    """Best-effort container duration and video dimensions in one ffprobe call.

    Returns ``(duration_seconds, width, height)``. A single invocation asks
    for the stream dimensions and the format duration at once
    (``-show_entries stream=width,height:format=duration``); the first stream
    carrying both a width and a height is the video stream. Unknown or
    unreadable values degrade to ``0.0`` / ``None`` (never an error): the box
    check is skipped when the dimensions are unknown.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(video_path),
            ],
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0, None, None
    if proc.returncode != 0:
        return 0.0, None, None
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return 0.0, None, None
    if not isinstance(payload, dict):
        return 0.0, None, None
    dims = _first_video_stream(payload.get("streams"))
    width, height = dims if dims is not None else (None, None)
    return _format_duration(payload.get("format")), width, height


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
    max_edge: int | None = None,
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
        max_edge: When given and smaller than the supported box long edge
            (``SUPPORTED_MAX_WIDTH``), frames are scaled in ffmpeg so the long
            edge is at most ``max_edge`` (aspect preserved, never upscaled,
            even dimensions) — the explicit opt-in to downscaling. ``None``
            (the default) leaves in-box frames at their source resolution
            (odd dimensions aligned to even); a source outside the supported
            box then fails with :class:`ExtractionFailed`.

    Returns:
        An :class:`app.models.ExtractionResult`; frames and candidates are
        both empty for a zero-frame video. ``source_width`` /
        ``source_height`` report the probed source resolution (0 when the
        probe could not determine it).

    Raises:
        ValueError: on non-positive/negative parameter values.
        ExtractionFailed: on missing file, unreadable/corrupt video, decode
            failure, timeout, or a source outside the supported resolution
            box without an explicit ``max_edge`` downscale opt-in.
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
    if max_edge is not None and (
        not isinstance(max_edge, int) or isinstance(max_edge, bool) or max_edge < 1
    ):
        raise ValueError(f"max_edge must be an integer >= 1 or None, got {max_edge!r}")

    path = Path(video_path)
    if not path.is_file():
        raise ExtractionFailed(f"video file not found: {path}")

    # Probe once (duration + source dimensions) before decoding: an out-of-box
    # source must fail fast without being decoded.
    duration, source_width, source_height = _probe_video_info(path, timeout_seconds)
    scale_box = _resolution_box(source_width, source_height, max_edge)
    result_width = source_width or 0
    result_height = source_height or 0

    qv = jpeg_quality_to_qv(jpeg_quality)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="video-dairy-sampler-") as tmp:
        tmp_dir = Path(tmp)
        logger.info(
            "Extracting frames from %s (%dx%d, probe_fps=%g, qv=%d, max_edge=%s, box=%s)",
            path,
            result_width,
            result_height,
            probe_fps,
            qv,
            max_edge,
            scale_box,
        )
        scene_scores, frames = _extract_pass(
            path, probe_fps, qv, scale_box, timeout_seconds, tmp_dir
        )
        if not frames:
            # Zero decodable frames: empty result, not an error.
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return ExtractionResult(
                frames=(),
                candidates=(),
                elapsed_ms=elapsed_ms,
                duration_seconds=0.0,
                source_width=result_width,
                source_height=result_height,
            )
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
        source_width=result_width,
        source_height=result_height,
    )
