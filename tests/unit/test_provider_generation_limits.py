from types import SimpleNamespace

from src.services.provider_generation_limits import (
    GenerationLimits,
    get_generation_limits,
    resolve_num_frames,
)


def _provider(extra: object) -> SimpleNamespace:
    return SimpleNamespace(extra_config_json=extra)


def test_resolve_num_frames_defaults_when_unset() -> None:
    assert resolve_num_frames(_provider({}), scene_default=120) == 120
    assert resolve_num_frames(_provider(None), scene_default=120) == 120
    assert resolve_num_frames(_provider({"num_frames": 0}), scene_default=120) == 120


def test_resolve_num_frames_uses_configured_value() -> None:
    assert resolve_num_frames(_provider({"num_frames": 240}), scene_default=120) == 240
    assert resolve_num_frames(_provider({"num_frames": "60"}), scene_default=120) == 60


def test_resolve_num_frames_clamps_to_bounds() -> None:
    assert resolve_num_frames(_provider({"num_frames": 9999}), scene_default=120) == 600
    assert resolve_num_frames(_provider({"num_frames": "800"}), scene_default=120) == 600


def test_get_generation_limits_reads_num_frames() -> None:
    limits = get_generation_limits(_provider({"num_frames": 90, "max_model_len": 4096}))
    assert limits.num_frames == 90
    assert limits.max_model_len == 4096
    assert GenerationLimits().num_frames is None


def test_input_tokens_is_available_for_callers() -> None:
    limits = GenerationLimits(max_model_len=4096, max_output_tokens=2048)
    assert limits.input_tokens(scene_default=8192) == 2048
    assert GenerationLimits().input_tokens(scene_default=8192) is None
