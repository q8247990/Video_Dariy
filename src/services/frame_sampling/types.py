"""Frame-sampling value types and the sampler protocol.

This module is a pure contract layer: the frame sampler runs as a
separate container, so the backend stays free of vision-heavy
third-party dependencies. All value objects are frozen dataclasses so
they can cross the process boundary unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

FRAME_SET_SOURCES: tuple[str, ...] = ("sampler", "cache")


@dataclass(frozen=True)
class FrameSample:
    """One sampled frame: its position in the clip and the JPEG payload."""

    t_seconds: float
    jpeg_bytes: bytes
    reason: str = "uniform"


@dataclass(frozen=True)
class FrameSet:
    """The complete output of one sampling request."""

    frames: tuple[FrameSample, ...]
    source: str = "sampler"
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if self.source not in FRAME_SET_SOURCES:
            raise ValueError(
                f"unknown FrameSet.source: {self.source!r}; expected one of {FRAME_SET_SOURCES}"
            )


@dataclass(frozen=True)
class SampleRequest:
    """Sampling request sent from the backend to the sampler service."""

    video_path: str
    probe_fps: float
    window_pre_seconds: float
    window_post_seconds: float
    min_frames: int
    max_frames: int
    max_edge: int
    jpeg_quality: int
    dedupe_phash_distance: int | None = None
    mode: str = "uniform"


@runtime_checkable
class FrameSampler(Protocol):
    """Port for the frame sampling service (implemented out-of-process)."""

    def sample(self, request: SampleRequest) -> FrameSet:
        """Sample frames from the video described by ``request``."""
        ...
