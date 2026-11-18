"""ONNX person detection with pure preprocessing helpers (todo 16).

The detector wraps a YOLOv8-style ONNX model whose single output has shape
``(1, 4 + C, N)``: rows ``cx, cy, w, h`` followed by one score row per class.
Only the *person* class (``PERSON_CLASS_ID = 0``) is emitted.

All thresholds are constructor parameters — the detection path never falls
back to hidden defaults, so the service configuration (todo 19) is the
single source of truth for confidence / area / IoU tuning.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np

from app.models import Detection

logger = logging.getLogger(__name__)

PERSON_CLASS_ID = 0
DEFAULT_INPUT_SIZE = 640


class DetectorUnavailable(RuntimeError):
    """The detection model file is missing, unreadable or not a valid model."""


class InferenceSession(Protocol):
    """Minimal ``onnxruntime.InferenceSession`` surface used by ``OnnxDetector``.

    Tests inject a fake implementing the same duck-typed interface.
    """

    def run(self, output_names: list[str], input_feed: dict[str, Any]) -> list[Any]:
        """Run inference for the given output names and input feed."""
        ...

    def get_inputs(self) -> list[Any]:
        """Model inputs; each element exposes a ``name`` attribute."""
        ...

    def get_outputs(self) -> list[Any]:
        """Model outputs; each element exposes a ``name`` attribute."""
        ...


# ---------------------------------------------------------------------------
# Pure helpers (no I/O, no NumPy in the public signatures)
# ---------------------------------------------------------------------------


def letterbox_size(
    src_w: int, src_h: int, input_size: int = DEFAULT_INPUT_SIZE
) -> tuple[int, int, float, float]:
    """Compute the centred letterbox layout for an ``input_size`` square canvas.

    Returns ``(new_w, new_h, pad_x, pad_y)`` where the source frame is scaled
    to ``new_w x new_h`` and centred inside the canvas with the given padding.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError(f"Frame size must be positive, got {src_w}x{src_h}")
    scale = min(input_size / src_w, input_size / src_h)
    new_w = int(round(src_w * scale))
    new_h = int(round(src_h * scale))
    return new_w, new_h, (input_size - new_w) / 2.0, (input_size - new_h) / 2.0


def xywh_to_xyxy(
    boxes: Sequence[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Convert centre ``x, y, w, h`` boxes to ``x1, y1, x2, y2`` corners."""
    return [
        (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0) for x, y, w, h in boxes
    ]


def box_iou(
    box_a: tuple[float, float, float, float], box_b: tuple[float, float, float, float]
) -> float:
    """Intersection-over-union of two ``x1, y1, x2, y2`` boxes (0.0 if none)."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = inter_w * inter_h
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - inter
    if union <= 0.0:
        return 0.0
    return inter / union


def nms(
    boxes: Sequence[tuple[float, float, float, float]],
    scores: Sequence[float],
    iou_threshold: float,
) -> list[int]:
    """Greedy NMS over ``x1, y1, x2, y2`` boxes.

    Returns the kept indices in score-descending order (ties broken by
    coordinates). A box is suppressed only when its IoU with an already-kept
    box is *strictly greater* than ``iou_threshold``; an IoU exactly equal to
    the threshold keeps both boxes, which makes the boundary deterministic.
    """
    order = sorted(
        range(len(boxes)),
        key=lambda i: (-scores[i], boxes[i][0], boxes[i][1], boxes[i][2], boxes[i][3]),
    )
    kept: list[int] = []
    for i in order:
        if all(box_iou(boxes[i], boxes[k]) <= iou_threshold for k in kept):
            kept.append(i)
    return kept


def filter_by_area(
    detections: Sequence[Detection],
    frame_w: int,
    frame_h: int,
    min_area_ratio: float,
) -> list[Detection]:
    """Keep detections whose box area is at least ``min_area_ratio`` of the frame."""
    frame_area = float(frame_w * frame_h)
    if frame_area <= 0.0:
        raise ValueError(f"Frame size must be positive, got {frame_w}x{frame_h}")
    return [
        d
        for d in detections
        if (d.x2 - d.x1) * (d.y2 - d.y1) / frame_area >= min_area_ratio
    ]


# ---------------------------------------------------------------------------
# Session loading
# ---------------------------------------------------------------------------


def _load_session(model_path: Path) -> InferenceSession:
    """Load the default CPU session, raising ``DetectorUnavailable`` on failure.

    ``onnxruntime`` is imported lazily so this module stays importable in
    environments without it (e.g. when only the pure helpers are exercised).
    There is no retry: a missing or broken model is a hard, logged error.
    """
    if not model_path.is_file():
        raise DetectorUnavailable(f"Detection model file not found: {model_path}")
    try:
        import onnxruntime
    except ImportError as exc:  # pragma: no cover - environment specific
        raise DetectorUnavailable(f"onnxruntime is not installed: {exc}") from exc
    try:
        return onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
    except Exception as exc:
        raise DetectorUnavailable(f"Failed to load ONNX model at {model_path}: {exc}") from exc


def detector_ready(model_path: str | Path) -> bool:
    """Return ``True`` when the model file exists and loads. Never raises.

    Used by ``/healthz``; an empty or missing path and an invalid model file
    both report ``False`` while the service stays healthy.
    """
    path = Path(str(model_path))
    if not path.is_file():
        return False
    try:
        _load_session(path)
    except DetectorUnavailable:
        return False
    except Exception:  # defensive: health checks must never raise
        logger.warning("detector_ready: unexpected failure for %s", path, exc_info=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class OnnxDetector:
    """Person detector backed by a YOLOv8-style ONNX model.

    Thresholds (``conf_threshold`` / ``min_area_ratio`` / ``iou_threshold``)
    come exclusively from the constructor so the service config remains the
    single source of truth (todo 19 wires them in).
    """

    def __init__(
        self,
        model_path: Path,
        conf_threshold: float,
        min_area_ratio: float,
        iou_threshold: float = 0.45,
        session: InferenceSession | None = None,
        input_size: int = DEFAULT_INPUT_SIZE,
    ) -> None:
        self.model_path = Path(model_path)
        self.conf_threshold = conf_threshold
        self.min_area_ratio = min_area_ratio
        self.iou_threshold = iou_threshold
        self.input_size = input_size
        # An injected session (tests) bypasses file loading entirely.
        self._session: InferenceSession = (
            session if session is not None else _load_session(self.model_path)
        )

    def detect(self, frame_rgb: np.ndarray, frame_w: int, frame_h: int) -> list[Detection]:
        """Detect persons in one RGB frame; boxes are in original-frame pixels."""
        new_w, new_h, pad_x, pad_y = letterbox_size(frame_w, frame_h, self.input_size)
        blob = self._preprocess(
            frame_rgb, frame_w, frame_h, new_w, new_h, int(pad_x), int(pad_y)
        )

        input_names = [i.name for i in self._session.get_inputs()]
        feed = {input_names[0]: blob} if input_names else {}
        output_names = [o.name for o in self._session.get_outputs()]
        raw = np.asarray(self._session.run(output_names, feed)[0])
        # raw: (1, 4 + C, N) — cx, cy, w, h, then one score row per class.
        if raw.ndim != 3 or raw.shape[1] < 4 + PERSON_CLASS_ID + 1:
            raise DetectorUnavailable(
                f"Unexpected model output shape {raw.shape}; expected (1, >=5, N)"
            )

        cx, cy, w, h = raw[0, 0], raw[0, 1], raw[0, 2], raw[0, 3]
        scores = raw[0, 4 + PERSON_CLASS_ID]

        boxes: list[tuple[float, float, float, float]] = []
        kept_scores: list[float] = []
        for i in np.flatnonzero(scores >= self.conf_threshold):
            # Canvas coordinates -> original frame coordinates, clamped.
            x1 = min(max(float(cx[i] - w[i] / 2.0) - pad_x, 0.0), float(frame_w))
            y1 = min(max(float(cy[i] - h[i] / 2.0) - pad_y, 0.0), float(frame_h))
            x2 = min(max(float(cx[i] + w[i] / 2.0) - pad_x, 0.0), float(frame_w))
            y2 = min(max(float(cy[i] + h[i] / 2.0) - pad_y, 0.0), float(frame_h))
            boxes.append((x1, y1, x2, y2))
            kept_scores.append(float(scores[i]))

        if not boxes:
            return []

        kept_idx = nms(boxes, kept_scores, self.iou_threshold)
        detections = [
            Detection(
                class_id=PERSON_CLASS_ID,
                score=kept_scores[i],
                x1=boxes[i][0],
                y1=boxes[i][1],
                x2=boxes[i][2],
                y2=boxes[i][3],
            )
            for i in kept_idx
        ]
        return filter_by_area(detections, frame_w, frame_h, self.min_area_ratio)

    def _preprocess(
        self,
        frame_rgb: np.ndarray,
        frame_w: int,
        frame_h: int,
        new_w: int,
        new_h: int,
        pad_x: int,
        pad_y: int,
    ) -> np.ndarray:
        """Letterbox the frame into a ``(1, 3, input_size, input_size)`` float blob.

        Resizing uses deterministic nearest-neighbour sampling (pure NumPy,
        no OpenCV), the canvas is padded with black, and the blob is
        normalised to ``[0, 1]`` in CHW layout.
        """
        img = np.ascontiguousarray(frame_rgb)
        if img.ndim != 3 or img.shape != (frame_h, frame_w, 3):
            raise ValueError(
                f"frame_rgb shape {img.shape} does not match {frame_h}x{frame_w}x3"
            )
        if (new_h, new_w) != (frame_h, frame_w):
            row_idx = np.floor(np.linspace(0, frame_h - 1, new_h)).astype(np.int64)
            col_idx = np.floor(np.linspace(0, frame_w - 1, new_w)).astype(np.int64)
            img = img[row_idx][:, col_idx]
        canvas = np.zeros((self.input_size, self.input_size, 3), dtype=img.dtype)
        canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = img
        return canvas.transpose(2, 0, 1)[np.newaxis, ...].astype(np.float32) / 255.0
