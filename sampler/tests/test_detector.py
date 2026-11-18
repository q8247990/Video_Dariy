"""Tests for app.detector (todo 16): pure helpers, detection, real-file init."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from app.detector import (
    PERSON_CLASS_ID,
    DetectorUnavailable,
    OnnxDetector,
    box_iou,
    detector_ready,
    filter_by_area,
    letterbox_size,
    nms,
    xywh_to_xyxy,
)
from app.models import Detection
from helpers import build_constant_onnx, make_yolo_output

FRAME_W = 640
FRAME_H = 480


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_letterbox_size_640x480() -> None:
    assert letterbox_size(640, 480, 640) == (640, 480, 0.0, 80.0)


def test_letterbox_size_1920x1080() -> None:
    assert letterbox_size(1920, 1080, 640) == (640, 360, 0.0, 140.0)


def test_letterbox_size_square_has_no_padding() -> None:
    assert letterbox_size(640, 640) == (640, 640, 0.0, 0.0)


def test_letterbox_size_portrait() -> None:
    assert letterbox_size(480, 640) == (480, 640, 80.0, 0.0)


def test_letterbox_size_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        letterbox_size(0, 480)


def test_xywh_to_xyxy() -> None:
    assert xywh_to_xyxy([(10.0, 20.0, 4.0, 8.0)]) == [(8.0, 16.0, 12.0, 24.0)]
    assert xywh_to_xyxy([]) == []


def test_box_iou_disjoint_is_zero() -> None:
    assert box_iou((0.0, 0.0, 10.0, 10.0), (20.0, 20.0, 30.0, 30.0)) == 0.0


def test_nms_heavily_overlapping_keeps_best_only() -> None:
    boxes = [(0.0, 0.0, 10.0, 10.0), (1.0, 1.0, 11.0, 11.0)]
    assert nms(boxes, [0.9, 0.8], 0.45) == [0]


def test_nms_disjoint_keeps_all() -> None:
    boxes = [(0.0, 0.0, 10.0, 10.0), (50.0, 50.0, 60.0, 60.0)]
    assert sorted(nms(boxes, [0.9, 0.8], 0.45)) == [0, 1]


def test_nms_iou_exactly_at_threshold_keeps_both() -> None:
    # IoU of (0,0,10,10) vs (0,0,10,20) is exactly 0.5 (100/200); suppression
    # is strict (>, not >=), so at threshold 0.5 both survive.
    boxes = [(0.0, 0.0, 10.0, 10.0), (0.0, 0.0, 10.0, 20.0)]
    assert sorted(nms(boxes, [0.9, 0.8], 0.5)) == [0, 1]
    assert nms(boxes, [0.9, 0.8], 0.4) == [0]


def _det(x1: float, y1: float, x2: float, y2: float, score: float = 0.9) -> Detection:
    return Detection(class_id=0, score=score, x1=x1, y1=y1, x2=x2, y2=y2)


def test_filter_by_area_drops_small_boxes() -> None:
    big = _det(0.0, 0.0, 100.0, 100.0)
    small = _det(0.0, 0.0, 5.0, 5.0)
    assert filter_by_area([big, small], 100, 100, 0.01) == [big]


def test_filter_by_area_ratio_exactly_at_threshold_is_kept() -> None:
    det = _det(0.0, 0.0, 10.0, 10.0)  # 100/10000 = exactly 0.01
    assert filter_by_area([det], 100, 100, 0.01) == [det]


# ---------------------------------------------------------------------------
# OnnxDetector with an injected fake session
# ---------------------------------------------------------------------------


class FakeSession:
    """Deterministic stand-in for ``onnxruntime.InferenceSession``."""

    def __init__(self, output: np.ndarray) -> None:
        self._output = output
        self.feeds: list[tuple[list[str], dict]] = []

    def run(self, output_names: list[str], input_feed: dict) -> list:
        self.feeds.append((list(output_names), dict(input_feed)))
        return [self._output]

    def get_inputs(self) -> list:
        return [SimpleNamespace(name="image")]

    def get_outputs(self) -> list:
        return [SimpleNamespace(name="detections")]


@pytest.fixture
def detector() -> OnnxDetector:
    return OnnxDetector(
        model_path=Path("fake.onnx"),  # unused: session is injected
        conf_threshold=0.3,
        min_area_ratio=0.001,
        iou_threshold=0.45,
        session=FakeSession(make_yolo_output()),
    )


def _frame() -> np.ndarray:
    return np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)


def test_detect_returns_three_persons_with_frame_coordinates(detector: OnnxDetector) -> None:
    dets = detector.detect(_frame(), FRAME_W, FRAME_H)

    assert [d.class_id for d in dets] == [PERSON_CLASS_ID] * 3
    assert all(d.track_id is None for d in dets)
    expected = [
        (240.0, 120.0, 400.0, 360.0),
        (60.0, 40.0, 140.0, 160.0),
        (490.0, 140.0, 590.0, 300.0),
    ]
    for det, (x1, y1, x2, y2) in zip(dets, expected, strict=True):
        assert (det.x1, det.y1, det.x2, det.y2) == pytest.approx((x1, y1, x2, y2), abs=1e-3)
    assert [d.score for d in dets] == pytest.approx([0.9, 0.8, 0.6], abs=1e-6)


def test_detect_drops_detection_below_conf_threshold(detector: OnnxDetector) -> None:
    # conf 0.3 drops the 0.2 box; conf 0.15 keeps it (4 boxes total).
    assert len(detector.detect(_frame(), FRAME_W, FRAME_H)) == 3
    looser = OnnxDetector(
        Path("fake.onnx"),
        conf_threshold=0.15,
        min_area_ratio=0.001,
        session=FakeSession(make_yolo_output()),
    )
    assert len(looser.detect(_frame(), FRAME_W, FRAME_H)) == 4


def test_detect_drops_detection_below_min_area_ratio() -> None:
    strict = OnnxDetector(
        Path("fake.onnx"),
        conf_threshold=0.3,
        min_area_ratio=0.001,
        session=FakeSession(make_yolo_output()),
    )
    loose = OnnxDetector(
        Path("fake.onnx"),
        conf_threshold=0.3,
        min_area_ratio=0.0,
        session=FakeSession(make_yolo_output()),
    )
    # With area filtering on, the 2x2 box is dropped; with ratio 0 it survives.
    assert len(strict.detect(_frame(), FRAME_W, FRAME_H)) == 3
    assert len(loose.detect(_frame(), FRAME_W, FRAME_H)) == 4


def test_detect_feeds_letterboxed_chw_blob(detector: OnnxDetector) -> None:
    detector.detect(_frame(), FRAME_W, FRAME_H)
    output_names, feed = detector._session.feeds[0]
    assert output_names == ["detections"]
    blob = feed["image"]
    assert blob.shape == (1, 3, 640, 640)
    assert blob.dtype == np.float32
    assert 0.0 <= float(blob.min()) <= float(blob.max()) <= 1.0


# ---------------------------------------------------------------------------
# Real ONNX model file (synthesised constant-output model, dev extra: onnx)
# ---------------------------------------------------------------------------


def test_detector_initialises_from_real_onnx_file(tmp_path: Path) -> None:
    model = build_constant_onnx(tmp_path / "person.onnx", make_yolo_output())
    det = OnnxDetector(model, conf_threshold=0.3, min_area_ratio=0.001)

    dets = det.detect(_frame(), FRAME_W, FRAME_H)

    assert len(dets) == 3
    assert [d.class_id for d in dets] == [PERSON_CLASS_ID] * 3
    assert (dets[0].x1, dets[0].y1, dets[0].x2, dets[0].y2) == pytest.approx(
        (240.0, 120.0, 400.0, 360.0), abs=1e-3
    )


def test_detector_unavailable_when_model_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.onnx"
    with pytest.raises(DetectorUnavailable, match="not found"):
        OnnxDetector(missing, conf_threshold=0.3, min_area_ratio=0.001)
    assert detector_ready(missing) is False
    assert detector_ready("") is False


def test_detector_unavailable_for_invalid_model_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not-a-real-model")
    with pytest.raises(DetectorUnavailable, match="Failed to load"):
        OnnxDetector(bad, conf_threshold=0.3, min_area_ratio=0.001)
    assert detector_ready(bad) is False


def test_detector_ready_true_for_valid_model_file(tmp_path: Path) -> None:
    model = build_constant_onnx(tmp_path / "person.onnx", make_yolo_output())
    assert detector_ready(model) is True
