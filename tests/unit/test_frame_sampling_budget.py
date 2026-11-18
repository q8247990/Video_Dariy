"""Unit tests for the frame budget derivation (plan todo 3).

Hand-verified arithmetic for the whole table (verify by hand, do NOT just
assert whatever the code returns):

  Source frame 640x480, policy ``FramePolicy()`` (max_edge=640,
  target_context_ratio=0.9, min_frames=4, max_frames=120, probe_fps=2.0):

  * fit_long_edge(640, 480, 640) keeps (640, 480) (long edge already <= 640)
  * tokens_per_image = (640//32) * (480//32) = 20 * 15 = 300
  * prompt_tokens_est = 800; duration = 60s -> duration ceiling
    max(1, ceil(60 * 2.0)) = 120
  * the output budget arrives ONLY via the ``max_output_tokens`` argument,
    which the tests fill with ``resolve_image_output_tokens(provider)`` —
    the single source. A provider with no extra config resolves to the
    scene default 4096.
  * the budget gate (plan rule): FramePlanImpossible when budget <
    min_frames * tokens_per_image = 4 * 300 = 1200

  context 65,536: usable = int(65536 * 0.9) = int(58982.4) = 58,982
      budget    = 58,982 - 800 - 4,096 = 54,086
      by_budget = 54,086 // 300 = 180  (180*300 = 54,000 <= 54,086 < 54,300 = 181*300)
      180 > max_frames 120 -> frames = 120, clamp_reason = "max"

  context 32,768: usable = int(32768 * 0.9) = int(29491.2) = 29,491
      budget    = 29,491 - 800 - 4,096 = 24,595
      by_budget = 24,595 // 300 = 81   (81*300 = 24,300 <= 24,595 < 24,600 = 82*300)
      81 is inside [4, 120] and below the 120 duration ceiling
      -> frames = 81, clamp_reason = "budget"   (matches the plan's 32K -> 81)

  context 8,192: usable = int(8192 * 0.9) = int(7372.8) = 7,372
      budget    = 7,372 - 800 - 4,096 = 2,476
      by_budget = 2,476 // 300 = 8      (8*300 = 2,400 <= 2,476 < 2,700 = 9*300)
      8 is inside [4, 120] -> frames = 8, clamp_reason = "budget"
      (The plan's illustrative "8K -> 11" is arithmetically unreachable under
       the plan's own formula: 11 frames need budget >= 11*300 = 3,300, while
       usable - output = 7,372 - 4,096 = 3,276 even with prompt_tokens_est = 0,
       so 8 is the true maximum. No fudging of ratio / token estimate / budget.)

  context 4,100 (output 4,096): usable = 3,690;
      budget = 3,690 - 800 - 4,096 = -1,206 < 1,200 -> FramePlanImpossible

  context 8,192 (output 8,192 via resolver): usable = 7,372;
      budget = 7,372 - 800 - 8,192 = -1,620 < 1,200 -> FramePlanImpossible

Impossibility semantics (plan rule): the budget must be able to afford
``min_frames``. Since ``by_budget < min_frames`` is mathematically
equivalent to ``budget < min_frames * tokens_per_image``,
``clamp_reason == "min"`` is unreachable from ``compute_frame_plan`` — the
gate raises first. ``"min"`` remains a valid ``FramePlan`` enum value for
forward compatibility only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import src.services.frame_sampling.budget as budget_module
from src.services.frame_sampling.budget import (
    FramePlan,
    FramePlanImpossible,
    compute_frame_plan,
)
from src.services.frame_sampling.policy import FramePolicy
from src.services.provider_generation_limits import resolve_image_output_tokens

POLICY = FramePolicy()
FRAME_W = 640
FRAME_H = 480
DURATION = 60.0
PROMPT_TOKENS = 800
TOKENS_PER_IMAGE = 300  # (640 // 32) * (480 // 32) = 20 * 15

# The output budget enters ONLY through the resolver (plan: single source).
DEFAULT_PROVIDER = SimpleNamespace(extra_config_json=None)  # -> scene default 4096
RESOLVED_OUTPUT = resolve_image_output_tokens(DEFAULT_PROVIDER)
assert RESOLVED_OUTPUT == 4096  # the hand arithmetic below assumes 4096


def _plan(
    context_size: int,
    *,
    duration_seconds: float = DURATION,
    max_output_tokens: int = RESOLVED_OUTPUT,
    duration_source: str = "probe",
) -> FramePlan:
    return compute_frame_plan(
        context_size=context_size,
        policy=POLICY,
        duration_seconds=duration_seconds,
        prompt_tokens_est=PROMPT_TOKENS,
        frame_width=FRAME_W,
        frame_height=FRAME_H,
        max_output_tokens=max_output_tokens,
        duration_source=duration_source,
    )


def _check_timestamps(plan: FramePlan, duration: float) -> None:
    """Full-span even spacing: len == frames, 0.0 first, last < duration, increasing."""
    assert len(plan.timestamps) == plan.frames
    assert plan.timestamps[0] == 0.0
    assert plan.timestamps[-1] < duration
    assert all(a < b for a, b in zip(plan.timestamps, plan.timestamps[1:], strict=False))


# ---------------------------------------------------------------- main table


@pytest.mark.parametrize(
    ("context_size", "frames", "reason", "budget_tokens"),
    [
        # 65536*0.9 = 58982.4 -> 58982; 58982-800-4096 = 54086; 54086//300 = 180
        # 180 > max_frames 120 -> clamped to 120, reason "max"
        (65536, 120, "max", 54086),
        # 32768*0.9 = 29491.2 -> 29491; 29491-800-4096 = 24595; 24595//300 = 81
        # 81 in [4, 120], duration ceiling 120 not binding -> reason "budget"
        (32768, 81, "budget", 24595),
        # 8192*0.9 = 7372.8 -> 7372; 7372-800-4096 = 2476; 2476//300 = 8
        # 8 in [4, 120] -> reason "budget"
        # (plan's illustrative "8K -> 11" is unreachable: 11 frames need budget
        #  >= 3,300, but 7372 - 4096 = 3,276 < 3,300 even with prompt = 0)
        (8192, 8, "budget", 2476),
    ],
)
def test_frame_plan_budget_table(
    context_size: int, frames: int, reason: str, budget_tokens: int
) -> None:
    plan = _plan(context_size)
    assert plan.tokens_per_image == TOKENS_PER_IMAGE
    assert plan.budget_tokens == budget_tokens
    assert plan.frames == frames
    assert plan.clamp_reason == reason
    assert plan.duration_source == "probe"
    _check_timestamps(plan, DURATION)


def test_table_case_1_full_span_spacing() -> None:
    # 120 frames over 60s -> step 60/120 = 0.5s: 0.0, 0.5, ..., 59.5
    plan = _plan(65536)
    assert plan.timestamps[0] == 0.0
    assert plan.timestamps[1] == 0.5
    assert plan.timestamps[-1] == 59.5


def test_table_case_3_full_span_spacing() -> None:
    # 8 frames over 60s -> step 60/8 = 7.5s: 0.0, 7.5, ..., 52.5
    # (NOT first-N-seconds truncation)
    plan = _plan(8192)
    assert plan.timestamps == tuple(i * 60.0 / 8.0 for i in range(8))
    assert plan.timestamps[-1] == 52.5


# ---------------------------------------------------------------- edge cases


def test_duration_ceiling_is_binding() -> None:
    # context 32768 -> by_budget 81 (inside [4, 120]); duration 10s, probe_fps 2.0
    # -> duration ceiling max(1, ceil(10 * 2.0)) = 20 < 81 -> 20 frames, "duration"
    plan = _plan(32768, duration_seconds=10.0)
    assert plan.frames == 20
    assert plan.clamp_reason == "duration"
    # 20 frames over 10s -> step 0.5s: last at 9.5 < 10.0
    assert plan.timestamps == tuple(i * 10.0 / 20.0 for i in range(20))
    assert plan.timestamps[-1] == 9.5
    _check_timestamps(plan, 10.0)


def test_compute_frame_plan_never_returns_min_reason() -> None:
    # "min" is unreachable: the budget gate raises before by_budget < min_frames
    # can ever survive (the two conditions are mathematically equivalent).
    rows = [(65536, 60.0), (32768, 60.0), (8192, 60.0), (32768, 10.0)]
    for context_size, duration in rows:
        plan = _plan(context_size, duration_seconds=duration)
        assert plan.clamp_reason != "min"


def test_min_clamp_reason_still_constructs() -> None:
    # the enum value is accepted on FramePlan for forward compatibility,
    # even though compute_frame_plan never emits it
    plan = FramePlan(
        frames=4,
        timestamps=(0.0, 15.0, 30.0, 45.0),
        tokens_per_image=300,
        budget_tokens=1200,
        clamp_reason="min",
    )
    assert plan.clamp_reason == "min"


def test_impossible_when_budget_below_min_frames() -> None:
    # 4100*0.9 = 3690; 3690-800-4096 = -1206 < min_frames*tokens_per_image = 1200
    with pytest.raises(FramePlanImpossible) as excinfo:
        _plan(4100)
    message = str(excinfo.value)
    # the numbers are named
    assert "-1206" in message
    assert "1200" in message
    # the remedies are named: raise ctx / lower max_output_tokens / lower min_frames
    assert "context_size" in message
    assert "max_output_tokens" in message
    assert "min_frames" in message


def test_impossible_when_budget_negative() -> None:
    # output cap 8192 arrives through the resolver, not a formula literal
    provider = SimpleNamespace(extra_config_json={"frame_sampling": {"max_output_tokens": 8192}})
    assert resolve_image_output_tokens(provider) == 8192
    # 8192*0.9 = 7372; 7372-800-8192 = -1620 < 1200 -> impossible
    with pytest.raises(FramePlanImpossible) as excinfo:
        _plan(8192, max_output_tokens=resolve_image_output_tokens(provider))
    message = str(excinfo.value)
    assert "-1620" in message
    assert "300" in message
    assert "context_size" in message
    assert "max_output_tokens" in message
    assert "min_frames" in message


def test_impossible_is_a_value_error() -> None:
    assert issubclass(FramePlanImpossible, ValueError)


def test_duration_source_fallback_is_preserved() -> None:
    plan = _plan(8192, duration_source="fallback")
    assert plan.duration_source == "fallback"
    assert plan.frames == 8
    assert plan.clamp_reason == "budget"


def test_frame_plan_rejects_unknown_clamp_reason() -> None:
    with pytest.raises(ValueError, match="clamp_reason"):
        FramePlan(
            frames=4,
            timestamps=(0.0, 15.0, 30.0, 45.0),
            tokens_per_image=300,
            budget_tokens=-1206,
            clamp_reason="bogus",
        )


def test_frame_plan_rejects_unknown_duration_source() -> None:
    with pytest.raises(ValueError, match="duration_source"):
        FramePlan(
            frames=4,
            timestamps=(0.0, 15.0, 30.0, 45.0),
            tokens_per_image=300,
            budget_tokens=-1206,
            clamp_reason="min",  # valid enum value, even if never emitted
            duration_source="guess",
        )


def test_max_output_tokens_is_required_and_validated() -> None:
    kwargs = {"policy": POLICY, "frame_width": FRAME_W, "frame_height": FRAME_H}
    base = {
        "context_size": 8192,
        "duration_seconds": DURATION,
        "prompt_tokens_est": PROMPT_TOKENS,
    }
    with pytest.raises(TypeError):
        compute_frame_plan(**base, **kwargs)  # missing max_output_tokens
    with pytest.raises(ValueError, match="max_output_tokens"):
        compute_frame_plan(max_output_tokens=0, **base, **kwargs)
    with pytest.raises(ValueError, match="max_output_tokens"):
        compute_frame_plan(max_output_tokens=-1, **base, **kwargs)
    with pytest.raises(ValueError, match="max_output_tokens"):
        compute_frame_plan(max_output_tokens=True, **base, **kwargs)  # bool rejected


def test_compute_frame_plan_validates_inputs() -> None:
    kwargs = {"policy": POLICY, "frame_width": FRAME_W, "frame_height": FRAME_H}
    base = {"prompt_tokens_est": PROMPT_TOKENS, "max_output_tokens": RESOLVED_OUTPUT}
    with pytest.raises(ValueError, match="context_size"):
        compute_frame_plan(context_size=0, duration_seconds=DURATION, **base, **kwargs)
    with pytest.raises(ValueError, match="context_size"):
        compute_frame_plan(context_size=True, duration_seconds=DURATION, **base, **kwargs)
    with pytest.raises(ValueError, match="duration_seconds"):
        compute_frame_plan(context_size=8192, duration_seconds=0.0, **base, **kwargs)
    with pytest.raises(ValueError, match="duration_seconds"):
        compute_frame_plan(context_size=8192, duration_seconds=True, **base, **kwargs)
    with pytest.raises(ValueError, match="duration_seconds"):
        compute_frame_plan(context_size=8192, duration_seconds="60", **base, **kwargs)
    with pytest.raises(ValueError, match="prompt_tokens_est"):
        compute_frame_plan(
            context_size=8192,
            duration_seconds=DURATION,
            prompt_tokens_est=-1,
            max_output_tokens=RESOLVED_OUTPUT,
            **kwargs,
        )
    with pytest.raises(ValueError, match="prompt_tokens_est"):
        compute_frame_plan(
            context_size=8192,
            duration_seconds=DURATION,
            prompt_tokens_est=True,
            max_output_tokens=RESOLVED_OUTPUT,
            **kwargs,
        )


# ------------------------------------------- budget/execute shared one source


def test_budget_and_execute_share_one_output_budget_source() -> None:
    # The resolver is the single source of the output budget: the plan formula
    # contains no literal 2048/4096.
    provider = SimpleNamespace(extra_config_json={"frame_sampling": {"max_output_tokens": 2048}})
    resolved = resolve_image_output_tokens(provider)
    assert resolved == 2048
    plan = compute_frame_plan(
        context_size=8192,
        policy=POLICY,
        duration_seconds=DURATION,
        prompt_tokens_est=PROMPT_TOKENS,
        frame_width=FRAME_W,
        frame_height=FRAME_H,
        max_output_tokens=resolved,
    )
    # usable = int(8192 * 0.9) = 7372; budget = usable - prompt - resolved
    # = 7372 - 800 - 2048 = 4524 (exactly the resolved cap is subtracted;
    # a leaked scene default of 4096 would give 2476)
    usable = int(8192 * POLICY.target_context_ratio)
    assert plan.budget_tokens == usable - PROMPT_TOKENS - resolved
    assert plan.budget_tokens == 4524
    # 4524 // 300 = 15 frames (within [4, 120])
    assert plan.frames == 15


def test_resolve_image_output_tokens_defaults_and_clamps() -> None:
    assert resolve_image_output_tokens(SimpleNamespace(extra_config_json=None)) == 4096
    assert resolve_image_output_tokens(SimpleNamespace(extra_config_json={})) == 4096
    bare = SimpleNamespace(extra_config_json={})
    assert resolve_image_output_tokens(bare, scene_default=1024) == 1024
    # clamped to max_model_len when set
    provider = SimpleNamespace(
        extra_config_json={
            "frame_sampling": {"max_output_tokens": 8192},
            "max_model_len": 2048,
        }
    )
    assert resolve_image_output_tokens(provider) == 2048


def test_no_literal_output_budget_in_budget_module() -> None:
    # guard: the output budget may only arrive via the max_output_tokens
    # argument — no bare literal may live in the formula's module
    source = Path(budget_module.__file__).read_text(encoding="utf-8")
    assert "2048" not in source
    assert "4096" not in source
