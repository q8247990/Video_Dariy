"""Deterministic IoU-based multi-object tracker (todo 16).

The tracker assigns stable, monotonically increasing ``track_id`` values to
detections of the same class across frames. Matching is greedy and
deterministic:

1. Tracks that have gone without a detection for more than ``max_age_ms``
   are dropped *before* matching, so a reappearing object always gets a new
   id once it has aged out.
2. Detections are processed in score-descending order (ties broken by
   coordinates); each is paired with the highest-IoU unmatched track at or
   above ``iou_threshold`` (ties broken by lower track id).
3. Unmatched detections open new tracks; unmatched (carried) tracks simply
   wait for ``max_age_ms`` without appearing in the output.

The output order is sorted by ``track_id`` so results are reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from app.detector import box_iou
from app.models import Detection


@dataclass
class _Track:
    """Internal state of a tracked object (active or being carried)."""

    track_id: int
    class_id: int
    box: tuple[float, float, float, float]
    score: float
    last_seen_ms: int


def _det_sort_key(d: Detection) -> tuple[float, float, float, float, float]:
    """Deterministic processing order: score desc, then coordinates."""
    return (-d.score, d.x1, d.y1, d.x2, d.y2)


class IouTracker:
    """Greedy IoU tracker with bounded carry-over for missed frames."""

    def __init__(self, max_age_ms: int = 1500, iou_threshold: float = 0.3) -> None:
        self.max_age_ms = max_age_ms
        self.iou_threshold = iou_threshold
        self._next_track_id = 1
        self._tracks: dict[int, _Track] = {}

    def update(self, detections: Sequence[Detection], timestamp_ms: int) -> list[Detection]:
        """Match one frame of detections; returns detections with ``track_id`` set."""
        # Age out first so reappearings after max_age_ms get fresh ids.
        self._age_out(timestamp_ms)
        assignments = self._match(detections)

        result: list[Detection] = []
        for track_id, det in assignments.items():
            track = self._tracks[track_id]
            track.box = (det.x1, det.y1, det.x2, det.y2)
            track.score = det.score
            track.last_seen_ms = timestamp_ms
            result.append(replace(det, track_id=track_id))

        result.extend(self._open_tracks(detections, assignments, timestamp_ms))
        result.sort(key=lambda d: d.track_id)
        return result

    def _age_out(self, timestamp_ms: int) -> None:
        """Drop carried tracks that went without a detection for too long."""
        for track_id in list(self._tracks):
            if timestamp_ms - self._tracks[track_id].last_seen_ms > self.max_age_ms:
                del self._tracks[track_id]

    def _match(self, detections: Sequence[Detection]) -> dict[int, Detection]:
        """Greedy, deterministic IoU matching (score desc, then coordinates)."""
        assignments: dict[int, Detection] = {}
        for det in sorted(detections, key=_det_sort_key):
            best_id: int | None = None
            best_iou = 0.0
            for track_id, track in self._tracks.items():
                if track_id in assignments or track.class_id != det.class_id:
                    continue
                iou = box_iou(track.box, (det.x1, det.y1, det.x2, det.y2))
                if iou >= self.iou_threshold and (
                    best_id is None
                    or iou > best_iou
                    or (iou == best_iou and track_id < best_id)
                ):
                    best_id, best_iou = track_id, iou
            if best_id is not None:
                assignments[best_id] = det
        return assignments

    def _open_tracks(
        self,
        detections: Sequence[Detection],
        assignments: dict[int, Detection],
        timestamp_ms: int,
    ) -> list[Detection]:
        """Open new tracks for unmatched detections in deterministic order."""
        result: list[Detection] = []
        for det in sorted(detections, key=_det_sort_key):
            if any(det is assigned for assigned in assignments.values()):
                continue
            track = _Track(
                track_id=self._next_track_id,
                class_id=det.class_id,
                box=(det.x1, det.y1, det.x2, det.y2),
                score=det.score,
                last_seen_ms=timestamp_ms,
            )
            self._next_track_id += 1
            self._tracks[track.track_id] = track
            result.append(replace(det, track_id=track.track_id))
        return result
