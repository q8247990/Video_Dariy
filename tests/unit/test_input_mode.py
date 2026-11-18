"""Unit tests for provider vision input-mode resolution (plan todo 9).

Covers every branch of :func:`src.services.frame_sampling.mode.resolve_input_mode`:
the backward-compatible ``video`` default (missing / non-dict
``extra_config_json``, absent ``frame_sampling``), the ``image`` mode (an
explicit ``frame_sampling`` dict, optionally ``enabled: true``), the explicit
``video`` opt-out (``enabled: false``), and the two warning branches
(``frame_sampling`` not a dict; ``enabled`` not a strict bool).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from src.services.frame_sampling.mode import INPUT_MODE_IMAGE, INPUT_MODE_VIDEO, resolve_input_mode

LOG_NAME = "src.services.frame_sampling.mode"


class _Provider:
    """Tiny stand-in for an ``LLMProvider`` row (only ``extra_config_json``)."""

    def __init__(self, extra_config_json: Any = None) -> None:
        self.extra_config_json = extra_config_json


def _mode(extra: Any) -> str:
    return resolve_input_mode(_Provider(extra))


def test_constants_have_expected_values() -> None:
    assert INPUT_MODE_IMAGE == "image"
    assert INPUT_MODE_VIDEO == "video"


# -------------------------------------------------------- video defaults


def test_video_when_extra_config_attribute_missing(caplog) -> None:
    class _Bare:
        pass

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert resolve_input_mode(_Bare()) == INPUT_MODE_VIDEO
    assert list(caplog.records) == []


@pytest.mark.parametrize("extra", [None, [1, 2], "frame_sampling", 42, object()])
def test_video_when_extra_config_not_a_dict(extra: Any, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert _mode(extra) == INPUT_MODE_VIDEO
    assert list(caplog.records) == []


def test_video_when_frame_sampling_absent(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert _mode({}) == INPUT_MODE_VIDEO
    assert list(caplog.records) == []


def test_video_when_frame_sampling_explicit_null() -> None:
    assert _mode({"frame_sampling": None}) == INPUT_MODE_VIDEO


def test_video_with_warning_when_frame_sampling_not_a_dict(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert _mode({"frame_sampling": "image"}) == INPUT_MODE_VIDEO
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "frame_sampling" in caplog.text


# ------------------------------------------------------------ image mode


def test_image_when_frame_sampling_dict_and_enabled_absent(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert _mode({"frame_sampling": {}}) == INPUT_MODE_IMAGE
    assert list(caplog.records) == []


def test_image_when_frame_sampling_dict_with_other_keys_only() -> None:
    assert _mode({"frame_sampling": {"preset": "dedupe", "max_frames": 60}}) == INPUT_MODE_IMAGE


def test_image_when_enabled_is_true() -> None:
    assert _mode({"frame_sampling": {"enabled": True}}) == INPUT_MODE_IMAGE


def test_video_when_enabled_is_false() -> None:
    assert _mode({"frame_sampling": {"enabled": False}}) == INPUT_MODE_VIDEO


@pytest.mark.parametrize("enabled", [1, 0, "true", "True", None, ["True"], object()])
def test_video_with_warning_when_enabled_not_a_strict_bool(enabled: Any, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert _mode({"frame_sampling": {"enabled": enabled}}) == INPUT_MODE_VIDEO
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "frame_sampling" in caplog.text
    assert "enabled" in caplog.text
