"""Plain data structures shared by the detector and the tracker (todo 16).

These types deliberately avoid NumPy: they cross the boundary between the
inference internals (``detector``) and the API/orchestration layer, where
only pure Python values are acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Detection:
    """A single detected person in one frame.

    ``x1/y1/x2/y2`` are in pixel coordinates of the original (un-letterboxed)
    frame. ``track_id`` is assigned by :class:`app.tracker.IouTracker` and
    stays ``None`` until a tracker has seen the detection.
    """

    class_id: int
    score: float
    x1: float
    y1: float
    x2: float
    y2: float
    track_id: int | None = None


@dataclass
class DetectionBatch:
    """All person detections of one frame plus tracker age in milliseconds."""

    detections: tuple[Detection, ...]
    tracker_age_ms: int = 0
