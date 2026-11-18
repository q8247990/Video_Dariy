"""Shared test helpers for todo 16.

Provides a deterministic YOLOv8-shaped network output and a tiny
constant-output ONNX model so tests can exercise the real onnxruntime path
without shipping a real detection model file.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


def make_yolo_output() -> np.ndarray:
    """A ``(1, 5, 8400)`` YOLOv8-style output with five candidate boxes.

    Candidates ``(cx, cy, w, h, score)`` in canvas pixels (640x640 letterbox
    of a 640x480 frame, so pad_y = 80):

    - 0: person A   (320, 320, 160, 240, 0.90) -> frame box (240, 120, 400, 360)
    - 1: person B   (100, 180,  80, 120, 0.80) -> frame box ( 60,  40, 140, 160)
    - 2: person C   (540, 300, 100, 160, 0.60) -> frame box (490, 140, 590, 300)
    - 3: weak box   ( 50, 200,  60,  60, 0.20) -> below conf_threshold 0.3
    - 4: tiny box   (600, 300,   2,   2, 0.95) -> below min_area_ratio 0.001
    """
    out = np.zeros((1, 5, 8400), dtype=np.float32)
    boxes = [
        (320.0, 320.0, 160.0, 240.0, 0.9),
        (100.0, 180.0, 80.0, 120.0, 0.8),
        (540.0, 300.0, 100.0, 160.0, 0.6),
        (50.0, 200.0, 60.0, 60.0, 0.2),
        (600.0, 300.0, 2.0, 2.0, 0.95),
    ]
    for i, (cx, cy, w, h, s) in enumerate(boxes):
        out[0, 0, i] = cx
        out[0, 1, i] = cy
        out[0, 2, i] = w
        out[0, 3, i] = h
        out[0, 4, i] = s
    return out


def build_constant_onnx(path: Path, output: np.ndarray) -> Path:
    """Write a minimal valid ONNX model: one float image input, constant output.

    The single graph node is a ``Constant`` returning ``output`` regardless of
    the input, which is enough to prove that ``OnnxDetector`` initialises from
    a real file and that ``detector_ready`` accepts a real model.
    """
    constant = helper.make_node(
        "Constant",
        inputs=[],
        outputs=["detections"],
        value=numpy_helper.from_array(output, name="detections"),
    )
    image = helper.make_tensor_value_info("image", TensorProto.FLOAT, [1, 3, 640, 640])
    det_out = helper.make_tensor_value_info("detections", TensorProto.FLOAT, [1, 5, 8400])
    graph = helper.make_graph(
        [constant], "const-person-detector", [image], [det_out]
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, path)
    return path
