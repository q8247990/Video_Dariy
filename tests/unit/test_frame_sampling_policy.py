"""Unit tests for per-model frame-sampling policy parsing (plan todo 2).

Covers the default value table, mode-bundle merging, legacy ``preset`` key
mapping, per-key override, boundary clamping, and malformed inputs (which
must clamp/ignore without raising).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from src.services.frame_sampling.policy import PRESETS, FramePolicy, parse_frame_policy


class _Provider:
    """Tiny stand-in for an ``LLMProvider`` row (only ``extra_config_json``)."""

    def __init__(self, extra_config_json: Any = None) -> None:
        self.extra_config_json = extra_config_json


def _policy(extra: Any) -> FramePolicy:
    return parse_frame_policy(_Provider(extra))


def _fs(*pairs: tuple[str, Any]) -> dict[str, Any]:
    """Build an ``extra_config_json`` with a ``frame_sampling`` sub-dict."""
    return {"frame_sampling": dict(pairs)}


# ---------------------------------------------------------------- defaults


def test_full_defaults_when_extra_config_is_none() -> None:
    assert _policy(None) == FramePolicy()


@pytest.mark.parametrize("extra", [[1, 2], "frame_sampling", 42, object()])
def test_full_defaults_when_extra_config_is_not_a_dict(extra: Any) -> None:
    assert _policy(extra) == FramePolicy()


def test_provider_without_extra_config_attribute() -> None:
    class _Bare:
        pass

    assert parse_frame_policy(_Bare()) == FramePolicy()


def test_default_value_table() -> None:
    policy = _policy(None)
    assert policy.mode == "event"
    assert policy.target_context_ratio == 0.9
    assert policy.max_edge == 2560
    assert policy.probe_fps == 2.0
    assert policy.min_frames == 4
    assert policy.max_frames == 120
    assert policy.jpeg_quality == 85
    assert policy.dedupe_phash_distance is None
    assert policy.window_pre_seconds == 5.0
    assert policy.window_post_seconds == 10.0
    assert policy.max_event_windows == 4
    assert policy.max_output_tokens == 2048
    assert policy.enabled is True
    assert policy.mad_threshold == 1.0
    assert policy.periodic_anchor_seconds == 8.0
    assert policy.activity_gate is True
    assert policy.static_change_ratio == 0.05
    assert policy.static_max_frames == 2


def test_empty_frame_sampling_subdict_is_all_defaults() -> None:
    assert _policy({"frame_sampling": {}}) == FramePolicy()


# --------------------------------------------------------------------- modes


@pytest.mark.parametrize("name", ["uniform", "dedupe", "event", "keyframe"])
def test_known_modes_are_registered(name: str) -> None:
    assert name in PRESETS
    assert isinstance(PRESETS[name], dict)


def test_uniform_and_event_share_numeric_defaults() -> None:
    # The built-in values *are* the event defaults; uniform is the same
    # reference point (the legacy raw-mp4 sampling behavior).
    assert PRESETS["uniform"] == PRESETS["event"]


def test_mode_supplies_defaults_for_missing_keys() -> None:
    policy = _policy(_fs(("mode", "dedupe")))
    assert policy.mode == "dedupe"
    for key, value in PRESETS["dedupe"].items():
        assert getattr(policy, key) == value


def test_explicit_key_overrides_mode_default() -> None:
    policy = _policy(_fs(("mode", "dedupe"), ("dedupe_phash_distance", 10)))
    assert policy.dedupe_phash_distance == 10


def test_explicit_null_disables_mode_default() -> None:
    # ``dedupe_phash_distance: null`` is a meaningful "off" and must beat the
    # mode bundle's value of 6.
    policy = _policy(_fs(("mode", "dedupe"), ("dedupe_phash_distance", None)))
    assert policy.dedupe_phash_distance is None


def test_keyframe_bundle_supplies_defaults_when_keys_absent() -> None:
    policy = _policy(_fs(("mode", "keyframe")))
    assert policy.mode == "keyframe"
    assert policy.probe_fps == 2.0
    assert policy.max_frames == 48
    assert policy.dedupe_phash_distance == 6
    assert policy.mad_threshold == 1.0
    assert policy.periodic_anchor_seconds == 8.0
    for key, value in PRESETS["keyframe"].items():
        assert getattr(policy, key) == value


def test_explicit_keys_override_keyframe_bundle() -> None:
    policy = _policy(
        _fs(
            ("mode", "keyframe"),
            ("probe_fps", 1.0),
            ("max_frames", 24),
            ("dedupe_phash_distance", 12),
            ("mad_threshold", 2.5),
            ("periodic_anchor_seconds", 16.0),
        )
    )
    assert policy.probe_fps == 1.0
    assert policy.max_frames == 24
    assert policy.dedupe_phash_distance == 12
    assert policy.mad_threshold == 2.5
    assert policy.periodic_anchor_seconds == 16.0


@pytest.mark.parametrize("mode", ["nope", 3, None, ["dedupe"]])
def test_unknown_or_malformed_mode_falls_back_to_event(mode: Any) -> None:
    assert _policy(_fs(("mode", mode))) == FramePolicy()


# -------------------------------------------------------- legacy preset key


@pytest.mark.parametrize(
    ("preset", "expected_mode"),
    [("baseline", "uniform"), ("dedupe", "dedupe"), ("event", "event")],
)
def test_legacy_preset_key_maps_to_mode(preset: str, expected_mode: str) -> None:
    policy = _policy(_fs(("preset", preset)))
    assert policy.mode == expected_mode


def test_legacy_preset_supplies_bundle_defaults_for_missing_keys() -> None:
    policy = _policy(_fs(("preset", "dedupe")))
    for key, value in PRESETS["dedupe"].items():
        assert getattr(policy, key) == value


def test_explicit_mode_wins_over_legacy_preset() -> None:
    policy = _policy(_fs(("mode", "uniform"), ("preset", "dedupe")))
    assert policy.mode == "uniform"
    # and the uniform bundle (no phash default) is applied, not dedupe's
    assert policy.dedupe_phash_distance is None


@pytest.mark.parametrize("preset", ["nope", 3, None, ["dedupe"]])
def test_unknown_or_malformed_legacy_preset_falls_back_to_event(preset: Any) -> None:
    assert _policy(_fs(("preset", preset))) == FramePolicy()


def test_legacy_preset_emits_one_line_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="src.services.frame_sampling.policy"):
        _policy(_fs(("preset", "dedupe")))
    assert len(caplog.records) == 1
    assert "preset" in caplog.records[0].message


def test_invalid_mode_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="src.services.frame_sampling.policy"):
        _policy(_fs(("mode", "nope")))
    assert len(caplog.records) == 1
    assert "mode" in caplog.records[0].message


# ------------------------------------------------------------ per-key override


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("mode", "uniform"),
        ("target_context_ratio", 0.75),
        ("max_edge", 1280),
        ("probe_fps", 4.0),
        ("min_frames", 8),
        ("max_frames", 240),
        ("jpeg_quality", 90),
        ("dedupe_phash_distance", 4),
        ("window_pre_seconds", 2.5),
        ("window_post_seconds", 20.0),
        ("max_event_windows", 6),
        ("max_output_tokens", 4096),
        ("enabled", False),
        ("mad_threshold", 2.0),
        ("periodic_anchor_seconds", 16.0),
        ("activity_gate", False),
        ("static_change_ratio", 0.2),
        ("static_max_frames", 4),
    ],
)
def test_per_key_override(key: str, value: object) -> None:
    policy = _policy(_fs((key, value)))
    assert getattr(policy, key) == value


def test_integral_values_coerced_for_float_fields() -> None:
    policy = _policy(_fs(("probe_fps", 4), ("target_context_ratio", 1)))
    assert policy.probe_fps == 4.0
    assert policy.target_context_ratio == 1.0


def test_numeric_strings_are_accepted() -> None:
    policy = _policy(_fs(("max_edge", "800"), ("min_frames", "8"), ("probe_fps", "0.5")))
    assert policy.max_edge == 800
    assert policy.min_frames == 8
    assert policy.probe_fps == 0.5


def test_explicit_null_falls_back_to_default_without_raising() -> None:
    policy = _policy(_fs(("target_context_ratio", None), ("jpeg_quality", None)))
    assert policy.target_context_ratio == 0.9
    assert policy.jpeg_quality == 85


# ------------------------------------------------------------------- clamping


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("target_context_ratio", 0.1, 0.5),
        ("target_context_ratio", 1.5, 1.0),
        ("max_edge", -1, 320),
        ("max_edge", 350, 320),
        ("max_edge", 650, 640),
        # 2560 is the 1440p box upper bound: clamped, never raised beyond it
        ("max_edge", 4000, 2560),
        ("max_edge", 2570, 2560),
        # snapped down to a multiple of 32 (2550 % 32 = 22 -> 2528)
        ("max_edge", 2550, 2528),
        ("probe_fps", 0.01, 0.25),
        ("probe_fps", 99, 10.0),
        ("min_frames", 0, 1),
        ("min_frames", 999, 60),
        ("max_frames", 0, 1),
        ("max_frames", 10000, 600),
        ("jpeg_quality", 10, 60),
        ("jpeg_quality", 1000, 95),
        ("window_pre_seconds", -5, 0.0),
        ("window_pre_seconds", 61, 60.0),
        ("window_post_seconds", 500, 120.0),
        ("max_event_windows", 0, 1),
        ("max_event_windows", 99, 10),
        ("max_output_tokens", 0, 2048),
        ("max_output_tokens", -3, 2048),
        ("dedupe_phash_distance", 0, 1),
        ("dedupe_phash_distance", 100, 32),
        ("mad_threshold", -1, 0.0),
        ("mad_threshold", 999, 50.0),
        ("periodic_anchor_seconds", -0.5, 0.0),
        ("periodic_anchor_seconds", 999, 60.0),
        ("static_change_ratio", -0.5, 0.0),
        ("static_change_ratio", 2, 1.0),
        ("static_max_frames", 0, 1),
        # within [1, 60]; the default max_frames (120) leaves it untouched
        ("static_max_frames", 99, 60),
    ],
)
def test_out_of_range_values_are_clamped(key: str, value: Any, expected: object) -> None:
    assert getattr(_policy(_fs((key, value))), key) == expected


def test_min_frames_clamped_down_to_max_frames() -> None:
    policy = _policy(_fs(("min_frames", 100), ("max_frames", 10)))
    assert policy.max_frames == 10
    assert policy.min_frames == 10


def test_static_max_frames_clamped_down_to_max_frames() -> None:
    # The static shrink must never exceed the clip's overall frame budget:
    # clamped, never raised, never an error.
    policy = _policy(_fs(("max_frames", 5), ("static_max_frames", 10)))
    assert policy.max_frames == 5
    assert policy.static_max_frames == 5


def test_static_max_frames_clamp_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="src.services.frame_sampling.policy"):
        _policy(_fs(("max_frames", 5), ("static_max_frames", 10)))
    messages = [record.message for record in caplog.records]
    assert any("static_max_frames" in message for message in messages)


# -------------------------------------------------------------- malformed input


@pytest.mark.parametrize(
    ("key", "bad_value", "expected"),
    [
        ("target_context_ratio", "abc", 0.9),
        ("target_context_ratio", float("nan"), 0.9),
        ("max_edge", "wide", 2560),
        ("max_edge", -1, 320),
        ("probe_fps", "fast", 2.0),
        ("min_frames", True, 4),
        ("min_frames", None, 4),
        ("max_frames", "many", 120),
        ("jpeg_quality", 1000, 95),
        ("jpeg_quality", "high", 85),
        ("dedupe_phash_distance", "x", None),
        ("dedupe_phash_distance", True, None),
        ("window_pre_seconds", "soon", 5.0),
        ("window_post_seconds", "later", 10.0),
        ("max_event_windows", "all", 4),
        ("max_event_windows", True, 4),
        ("max_output_tokens", "lots", 2048),
        ("enabled", "yes", True),
        ("enabled", 1, True),
        ("mad_threshold", "abc", 1.0),
        ("mad_threshold", None, 1.0),
        ("periodic_anchor_seconds", "soon", 8.0),
        ("periodic_anchor_seconds", None, 8.0),
        ("activity_gate", "yes", True),
        ("activity_gate", 1, True),
        ("static_change_ratio", "abc", 0.05),
        ("static_change_ratio", float("nan"), 0.05),
        ("static_change_ratio", None, 0.05),
        ("static_max_frames", "many", 2),
        ("static_max_frames", True, 2),
        ("static_max_frames", None, 2),
    ],
)
def test_malformed_values_clamp_or_ignore_without_raising(
    key: str, bad_value: Any, expected: object
) -> None:
    policy = _policy(_fs((key, bad_value)))
    assert getattr(policy, key) == expected


def test_non_dict_frame_sampling_subdict_ignores_entire_config() -> None:
    assert _policy({"frame_sampling": "dedupe"}) == FramePolicy()
    assert _policy({"frame_sampling": [1, 2]}) == FramePolicy()


def test_mixed_valid_and_invalid_keys_resolve_independently() -> None:
    policy = _policy(
        _fs(
            ("max_edge", -1),
            ("jpeg_quality", 90),
            ("min_frames", True),
        )
    )
    assert policy.max_edge == 320
    assert policy.jpeg_quality == 90
    assert policy.min_frames == 4


# -------------------------------------------------------------------- logging


def test_clamped_value_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="src.services.frame_sampling.policy"):
        _policy(_fs(("max_edge", -1), ("min_frames", True)))
    messages = [record.message for record in caplog.records]
    assert any("max_edge" in message for message in messages)
    assert any("min_frames" in message for message in messages)


def test_valid_config_emits_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="src.services.frame_sampling.policy"):
        _policy(_fs(("max_edge", 1280), ("min_frames", 8), ("probe_fps", 1.5)))
    assert caplog.records == []
