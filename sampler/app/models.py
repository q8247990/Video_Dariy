"""Plain data structures shared by the sampler modules (todo 17).

These types deliberately avoid NumPy: they cross the boundary between the
extraction path (``extract``) and the API/orchestration layer, where only
pure Python values are acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExtractedFrame:
    """One JPEG frame decoded from a video at ``t_seconds``.

    ``t_seconds`` is the frame time in seconds relative to the start of the
    video; ``jpeg_bytes`` is the complete JPEG file (SOI through EOI marker).
    """

    t_seconds: float
    jpeg_bytes: bytes


@dataclass
class CandidateWindow:
    """A time window likely to contain an event, ordered by ``t_start``.

    ``reasons`` records how the window was created: ``("scene",)`` for a
    single scene-change trigger and ``("merged",)`` for a window that merged
    several trigger frames within the merge gap.
    """

    t_start: float
    t_end: float
    reasons: tuple[str, ...] = ()


@dataclass
class ExtractionResult:
    """Outcome of the two-pass ffmpeg extraction over one video file.

    ``frames`` is the complete probe-rate JPEG stream; ``candidates`` are the
    scene-gated event windows. An empty/zero-frame video yields empty tuples
    (not an error). ``elapsed_ms`` covers both ffmpeg passes.
    ``source_width`` / ``source_height`` report the probed source resolution
    (0 when the probe could not determine it).
    """

    frames: tuple[ExtractedFrame, ...]
    candidates: tuple[CandidateWindow, ...]
    elapsed_ms: int
    duration_seconds: float
    source_width: int = 0
    source_height: int = 0
