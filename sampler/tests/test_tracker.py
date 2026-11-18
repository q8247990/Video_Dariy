"""Tests for app.tracker.IouTracker (todo 16)."""

from __future__ import annotations

from app.models import Detection
from app.tracker import IouTracker


def det(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    score: float = 0.9,
    track_id: int | None = None,
) -> Detection:
    return Detection(
        class_id=0, score=score, x1=x1, y1=y1, x2=x2, y2=y2, track_id=track_id
    )


def test_object_keeps_track_id_across_three_frames() -> None:
    tracker = IouTracker(max_age_ms=1500, iou_threshold=0.3)
    r1 = tracker.update([det(100, 100, 200, 300)], 0)
    r2 = tracker.update([det(105, 105, 205, 305)], 100)
    r3 = tracker.update([det(110, 110, 210, 310)], 200)
    assert [d.track_id for d in r1] == [1]
    assert [d.track_id for d in r2] == [1]
    assert [d.track_id for d in r3] == [1]


def test_second_object_gets_different_id() -> None:
    tracker = IouTracker()
    r1 = tracker.update(
        [det(100, 100, 200, 300, score=0.9), det(400, 100, 500, 300, score=0.8)], 0
    )
    assert sorted(d.track_id for d in r1) == [1, 2]
    # The higher-scored object is processed first and gets the lower id.
    by_x1 = {d.x1: d.track_id for d in r1}
    assert by_x1[100.0] == 1
    assert by_x1[400.0] == 2
    # Both ids stay stable on the next frame.
    r2 = tracker.update(
        [det(102, 102, 202, 302, score=0.9), det(402, 102, 502, 302, score=0.8)], 100
    )
    by_x1 = {d.x1: d.track_id for d in r2}
    assert by_x1[102.0] == 1
    assert by_x1[402.0] == 2


def test_track_carried_within_max_age_keeps_its_id() -> None:
    tracker = IouTracker(max_age_ms=1000)
    assert [d.track_id for d in tracker.update([det(100, 100, 200, 300)], 0)] == [1]
    assert tracker.update([], 400) == []  # carried, nothing emitted
    r = tracker.update([det(100, 100, 200, 300)], 800)  # reappears within max_age
    assert [d.track_id for d in r] == [1]


def test_track_dropped_after_max_age_and_reappearance_gets_new_id() -> None:
    tracker = IouTracker(max_age_ms=1000)
    tracker.update([det(100, 100, 200, 300)], 0)
    assert tracker.update([], 1500) == []  # aged out: 1500 > 1000
    r = tracker.update([det(100, 100, 200, 300)], 2000)
    assert [d.track_id for d in r] == [2]  # new id, old track is gone


def test_no_detections_returns_empty_list() -> None:
    tracker = IouTracker()
    assert tracker.update([], 0) == []


def test_assignment_is_deterministic_across_runs() -> None:

    def run() -> list[tuple[float, int]]:
        t = IouTracker()
        r = t.update(
            [det(0, 0, 100, 100, score=0.7), det(50, 50, 150, 150, score=0.9)], 0
        )
        return [(d.x1, d.track_id) for d in r]

    assert run() == run()
    # Higher score processed first -> lower id.
    assert run() == [(50.0, 1), (0.0, 2)]
