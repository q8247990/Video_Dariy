"""Unit tests for the frame budget derivation (plan todo 3).

The plan is charged at the 1440p 16:9 box upper bound (ADR 0014): the
sampler does not scale sources inside the ``max_edge`` box, so the largest
frame a plan can receive is the 16:9 box itself.

Hand-verified arithmetic for the whole table (verify by hand, do NOT just
assert whatever the code returns):

  Frame box 2560x1440 (= max_edge x max_edge*9//16 at the default
  max_edge=2560), policy ``FramePolicy()`` (max_edge=2560,
  target_context_ratio=0.9, min_frames=4, max_frames=120, probe_fps=2.0):

  * fit_long_edge(2560, 1440, 2560) keeps (2560, 1440) (long edge == 2560)
  * tokens_per_image = (2560//32) * (1440//32) = 80 * 45 = 3600
  * prompt_tokens_est = 800; duration = 60s -> duration ceiling
    max(1, ceil(60 * 2.0)) = 120
  * the output budget arrives ONLY via the ``max_output_tokens`` argument,
    which the tests fill with ``resolve_image_output_tokens(provider)`` —
    the single source. A provider with no extra config resolves to the
    scene default 4096.
  * the budget gate (plan rule): FramePlanImpossible when budget <
    min_frames * tokens_per_image = 4 * 3600 = 14400

  context 262,144: usable = int(262144 * 0.9) = int(235929.6) = 235,929
      budget    = 235,929 - 800 - 4,096 = 231,033
      by_budget = 231,033 // 3600 = 64   (64*3600 = 230,400 <= 231,033 < 234,000 = 65*3600)
      64 is inside [4, 120] and below the 120 duration ceiling
      -> frames = 64, clamp_reason = "budget"

  context 131,072: usable = int(117964.8) = 117,964
      budget    = 117,964 - 800 - 4,096 = 113,068
      by_budget = 113,068 // 3600 = 31   (31*3600 = 111,600 <= 113,068 < 115,200 = 32*3600)
      -> frames = 31, clamp_reason = "budget"

  context 65,536: usable = int(58982.4) = 58,982
      budget    = 58,982 - 800 - 4,096 = 54,086
      by_budget = 54,086 // 3600 = 15    (15*3600 = 54,000 <= 54,086 < 57,600 = 16*3600)
      -> frames = 15, clamp_reason = "budget"

  context 32,768: usable = int(29491.2) = 29,491
      budget    = 29,491 - 800 - 4,096 = 24,595
      by_budget = 24,595 // 3600 = 6     (6*3600 = 21,600 <= 24,595 < 25,200 = 7*3600)
      -> frames = 6, clamp_reason = "budget"

  context 524,288: usable = int(471859.2) = 471,859
      budget    = 471,859 - 800 - 4,096 = 466,963
      by_budget = 466,963 // 3600 = 129  (129*3600 = 464,400 <= 466,963 < 468,000 = 130*3600)
      129 > max_frames 120 -> frames = 120, clamp_reason = "max"

  context 8,192: usable = int(7372.8) = 7,372
      budget    = 7,372 - 800 - 4,096 = 2,476
      by_budget = 2,476 // 3600 = 0 < min_frames 4
      -> FramePlanImpossible (the 1440p box makes 8K contexts
         unaffordable by default)

  context 8,192 (output 8,192 via resolver): usable = 7,372;
      budget = 7,372 - 800 - 8,192 = -1,620 < 14,400 -> FramePlanImpossible

Smaller box (opt-in downscale): max_edge=640 -> box 640x360,
tokens_per_image = 20 * 11 = 220 (the superseded square bound for the same
edge was (640//32)**2 = 400):

  context 32,768: budget 24,595 // 220 = 111 (111*220 = 24,420 <= 24,595 < 24,640 = 112*220)
      -> frames = 111, clamp_reason = "budget"

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
    conservative_tokens_per_image,
)
from src.services.frame_sampling.policy import FramePolicy
from src.services.provider_generation_limits import resolve_image_output_tokens

POLICY = FramePolicy()
FRAME_W = 2560  # 1440p 16:9 box long edge (default max_edge)
FRAME_H = 1440  # 2560 * 9 // 16
DURATION = 60.0
PROMPT_TOKENS = 800
TOKENS_PER_IMAGE = 3600  # (2560 // 32) * (1440 // 32) = 80 * 45

# The output budget enters ONLY through the resolver (plan: single source).
DEFAULT_PROVIDER = SimpleNamespace(extra_config_json=None)  # -> scene default 4096
RESOLVED_OUTPUT = resolve_image_output_tokens(DEFAULT_PROVIDER)
assert RESOLVED_OUTPUT == 4096  # the hand arithmetic below assumes 4096


def _plan(
    context_size: int,
    *,
    policy: FramePolicy = POLICY,
    duration_seconds: float = DURATION,
    max_output_tokens: int = RESOLVED_OUTPUT,
    duration_source: str = "probe",
) -> FramePlan:
    return compute_frame_plan(
        context_size=context_size,
        policy=policy,
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
        # 262144*0.9 = 235929.6 -> 235929; 235929-800-4096 = 231033; 231033//3600 = 64
        # 64 in [4, 120], duration ceiling 120 not binding -> reason "budget"
        (262144, 64, "budget", 231033),
        # 131072*0.9 = 117964.8 -> 117964; 117964-800-4096 = 113068; 113068//3600 = 31
        (131072, 31, "budget", 113068),
        # 65536*0.9 = 58982.4 -> 58982; 58982-800-4096 = 54086; 54086//3600 = 15
        (65536, 15, "budget", 54086),
        # 32768*0.9 = 29491.2 -> 29491; 29491-800-4096 = 24595; 24595//3600 = 6
        (32768, 6, "budget", 24595),
        # 524288*0.9 = 471859.2 -> 471859; 471859-800-4096 = 466963; 466963//3600 = 129
        # 129 > max_frames 120 -> clamped to 120, reason "max"
        (524288, 120, "max", 466963),
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


def test_table_max_clamp_full_span_spacing() -> None:
    # 120 frames over 60s -> step 60/120 = 0.5s: 0.0, 0.5, ..., 59.5
    plan = _plan(524288)
    assert plan.timestamps[0] == 0.0
    assert plan.timestamps[1] == 0.5
    assert plan.timestamps[-1] == 59.5


def test_table_case_32k_full_span_spacing() -> None:
    # 6 frames over 60s -> step 60/6 = 10.0s: 0.0, 10.0, ..., 50.0
    # (NOT first-N-seconds truncation)
    plan = _plan(32768)
    assert plan.timestamps == tuple(i * 60.0 / 6.0 for i in range(6))
    assert plan.timestamps[-1] == 50.0


# ------------------------------------------------------ 1440p box upper bound


def test_conservative_tokens_per_image_is_1440p_box_upper_bound() -> None:
    assert conservative_tokens_per_image(max_edge=2560) == 3600
    # opt-in downscale: the same formula at a smaller edge yields a smaller bound
    assert conservative_tokens_per_image(max_edge=640) == 220  # 20 * (360 // 32)
    assert conservative_tokens_per_image(max_edge=320) == 50  # 10 * (180 // 32)
    # one tile wide, less than one 16:9 row -> still >= 1
    assert conservative_tokens_per_image(max_edge=32) == 1
    with pytest.raises(ValueError):
        conservative_tokens_per_image(max_edge=31)  # below one tile edge
    with pytest.raises(ValueError):
        conservative_tokens_per_image(max_edge=0)
    with pytest.raises(ValueError):
        conservative_tokens_per_image(max_edge=True)  # type: ignore[arg-type]


def test_smaller_max_edge_box_plans_still_work() -> None:
    # max_edge=640 (opt-in downscale): box 640x360, 20 * 11 = 220 tokens/frame
    # (the superseded square bound for the same edge was (640//32)**2 = 400)
    small_policy = FramePolicy(max_edge=640)
    plan = compute_frame_plan(
        context_size=32768,
        policy=small_policy,
        duration_seconds=DURATION,
        prompt_tokens_est=PROMPT_TOKENS,
        frame_width=640,
        frame_height=640 * 9 // 16,
        max_output_tokens=RESOLVED_OUTPUT,
    )
    assert plan.tokens_per_image == 220
    # 32768*0.9 = 29491; 29491-800-4096 = 24595; 24595//220 = 111
    # (111*220 = 24420 <= 24595 < 24640 = 112*220) -> in [4, 120]
    assert plan.frames == 111
    assert plan.clamp_reason == "budget"
    _check_timestamps(plan, DURATION)


# ---------------------------------------------------------------- edge cases


def test_duration_ceiling_is_binding() -> None:
    # context 262144 -> by_budget 64 (inside [4, 120]); duration 10s, probe_fps 2.0
    # -> duration ceiling max(1, ceil(10 * 2.0)) = 20 < 64 -> 20 frames, "duration"
    plan = _plan(262144, duration_seconds=10.0)
    assert plan.frames == 20
    assert plan.clamp_reason == "duration"
    # 20 frames over 10s -> step 0.5s: last at 9.5 < 10.0
    assert plan.timestamps == tuple(i * 10.0 / 20.0 for i in range(20))
    assert plan.timestamps[-1] == 9.5
    _check_timestamps(plan, 10.0)


def test_compute_frame_plan_never_returns_min_reason() -> None:
    # "min" is unreachable: the budget gate raises before by_budget < min_frames
    # can ever survive (the two conditions are mathematically equivalent).
    rows = [(32768, 60.0), (65536, 60.0), (262144, 10.0), (524288, 60.0)]
    for context_size, duration in rows:
        plan = _plan(context_size, duration_seconds=duration)
        assert plan.clamp_reason != "min"


def test_min_clamp_reason_still_constructs() -> None:
    # the enum value is accepted on FramePlan for forward compatibility,
    # even though compute_frame_plan never emits it
    plan = FramePlan(
        frames=4,
        timestamps=(0.0, 15.0, 30.0, 45.0),
        tokens_per_image=3600,
        budget_tokens=14400,
        clamp_reason="min",
    )
    assert plan.clamp_reason == "min"


def test_impossible_when_budget_below_min_frames() -> None:
    # 8192*0.9 = 7372; 7372-800-4096 = 2476 < min_frames*tokens_per_image = 14400
    # (the 1440p box makes 8K contexts unaffordable by default)
    with pytest.raises(FramePlanImpossible) as excinfo:
        _plan(8192)
    message = str(excinfo.value)
    # the numbers are named
    assert "2476" in message
    assert "14400" in message
    # the remedies are named: raise ctx / lower max_output_tokens / lower min_frames
    assert "context_size" in message
    assert "max_output_tokens" in message
    assert "min_frames" in message


def test_impossible_when_budget_negative() -> None:
    # output cap 8192 arrives through the resolver, not a formula literal
    provider = SimpleNamespace(extra_config_json={"frame_sampling": {"max_output_tokens": 8192}})
    assert resolve_image_output_tokens(provider) == 8192
    # 8192*0.9 = 7372; 7372-800-8192 = -1620 < 14400 -> impossible
    with pytest.raises(FramePlanImpossible) as excinfo:
        _plan(8192, max_output_tokens=resolve_image_output_tokens(provider))
    message = str(excinfo.value)
    assert "-1620" in message
    assert "3600" in message
    assert "context_size" in message
    assert "max_output_tokens" in message
    assert "min_frames" in message


def test_impossible_is_a_value_error() -> None:
    assert issubclass(FramePlanImpossible, ValueError)


def test_duration_source_fallback_is_preserved() -> None:
    plan = _plan(65536, duration_source="fallback")
    assert plan.duration_source == "fallback"
    assert plan.frames == 15
    assert plan.clamp_reason == "budget"


def test_frame_plan_rejects_unknown_clamp_reason() -> None:
    with pytest.raises(ValueError, match="clamp_reason"):
        FramePlan(
            frames=4,
            timestamps=(0.0, 15.0, 30.0, 45.0),
            tokens_per_image=3600,
            budget_tokens=-1620,
            clamp_reason="bogus",
        )


def test_frame_plan_rejects_unknown_duration_source() -> None:
    with pytest.raises(ValueError, match="duration_source"):
        FramePlan(
            frames=4,
            timestamps=(0.0, 15.0, 30.0, 45.0),
            tokens_per_image=3600,
            budget_tokens=-1620,
            clamp_reason="min",  # valid enum value, even if never emitted
            duration_source="guess",
        )


def test_max_output_tokens_is_required_and_validated() -> None:
    kwargs = {"policy": POLICY, "frame_width": FRAME_W, "frame_height": FRAME_H}
    base = {
        "context_size": 65536,
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
        compute_frame_plan(context_size=65536, duration_seconds=0.0, **base, **kwargs)
    with pytest.raises(ValueError, match="duration_seconds"):
        compute_frame_plan(context_size=65536, duration_seconds=True, **base, **kwargs)
    with pytest.raises(ValueError, match="duration_seconds"):
        compute_frame_plan(context_size=65536, duration_seconds="60", **base, **kwargs)
    with pytest.raises(ValueError, match="prompt_tokens_est"):
        compute_frame_plan(
            context_size=65536,
            duration_seconds=DURATION,
            prompt_tokens_est=-1,
            max_output_tokens=RESOLVED_OUTPUT,
            **kwargs,
        )
    with pytest.raises(ValueError, match="prompt_tokens_est"):
        compute_frame_plan(
            context_size=65536,
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
        context_size=32768,
        policy=POLICY,
        duration_seconds=DURATION,
        prompt_tokens_est=PROMPT_TOKENS,
        frame_width=FRAME_W,
        frame_height=FRAME_H,
        max_output_tokens=resolved,
    )
    # usable = int(32768 * 0.9) = 29491; budget = usable - prompt - resolved
    # = 29491 - 800 - 2048 = 26643 (exactly the resolved cap is subtracted;
    # a leaked scene default of 4096 would give 24595 -> 6 frames, not 7)
    usable = int(32768 * POLICY.target_context_ratio)
    assert plan.budget_tokens == usable - PROMPT_TOKENS - resolved
    assert plan.budget_tokens == 26643
    # 26643 // 3600 = 7 frames (within [4, 120])
    assert plan.frames == 7


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
