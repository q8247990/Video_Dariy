"""Frame budget derivation: how many frames fit in the model's token budget.

Given the model context window, the prompt size, the per-image token cost
(:mod:`src.services.frame_sampling.tokens`) and the provider output cap,
compute how many evenly spaced frames to sample from a clip of the given
duration. The frame count is clamped by four limits:

1. the token budget — ``int(context_size * target_context_ratio)`` minus the
   prompt and output caps, divided by the per-image token cost;
2. the policy ``min_frames`` floor;
3. the policy ``max_frames`` ceiling;
4. the duration ceiling — ``max(1, ceil(duration * probe_fps))``: a clip
   cannot yield more sampled frames than its duration times the probe rate.

The budget must be able to afford at least ``min_frames`` images; otherwise
:class:`FramePlanImpossible` is raised (fail fast rather than overspend the
model's context window).

Timestamps are always spread over the **full** clip (``i * duration /
frames``); never a first-N-seconds truncation. The output cap is a required
argument supplied via
:func:`src.services.provider_generation_limits.resolve_image_output_tokens`
so budget and execute subtract exactly the same number; this module carries
no default and no literal output budget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.tokens import TILE_EDGE, estimate_image_tokens, fit_long_edge

_CLAMP_REASONS: tuple[str, ...] = ("budget", "min", "max", "duration")
_DURATION_SOURCES: tuple[str, ...] = ("probe", "fallback")


class FramePlanImpossible(ValueError):
    """The token budget cannot afford even a single frame."""


@dataclass(frozen=True)
class FramePlan:
    """Resolved sampling plan: frame count, timestamps and the token budget.

    ``clamp_reason`` names the limit that bound the final frame count.
    ``"min"`` is accepted for forward compatibility but is currently never
    emitted by :func:`compute_frame_plan`: the budget gate raises
    :class:`FramePlanImpossible` when the budget cannot afford
    ``min_frames``, and that condition is exactly ``by_budget < min_frames``.
    """

    frames: int
    timestamps: tuple[float, ...]
    tokens_per_image: int
    budget_tokens: int
    clamp_reason: str
    duration_source: str = "probe"

    def __post_init__(self) -> None:
        if self.clamp_reason not in _CLAMP_REASONS:
            raise ValueError(
                f"FramePlan.clamp_reason must be one of {_CLAMP_REASONS}, got {self.clamp_reason!r}"
            )
        if self.duration_source not in _DURATION_SOURCES:
            raise ValueError(
                f"FramePlan.duration_source must be one of {_DURATION_SOURCES}, "
                f"got {self.duration_source!r}"
            )


def _require_int(value: object, name: str) -> None:
    """Reject bools and non-int values with a clear message."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")


def compute_frame_plan(
    *,
    context_size: int,
    policy: FramePolicy,
    duration_seconds: float,
    prompt_tokens_est: int,
    frame_width: int,
    frame_height: int,
    max_output_tokens: int,
    duration_source: str = "probe",
) -> FramePlan:
    """Derive the frame plan for one clip under ``policy``.

    ``max_output_tokens`` (the provider output cap) is REQUIRED and must be
    the result of
    :func:`src.services.provider_generation_limits.resolve_image_output_tokens`
    so the execute path and the budget subtract exactly the same number; this
    formula deliberately carries no default output budget. Raises
    :class:`FramePlanImpossible` when the budget cannot afford
    ``policy.min_frames`` images.
    """
    _require_int(context_size, "context_size")
    if context_size <= 0:
        raise ValueError(f"context_size must be > 0, got {context_size}")
    _require_int(prompt_tokens_est, "prompt_tokens_est")
    if prompt_tokens_est < 0:
        raise ValueError(f"prompt_tokens_est must be >= 0, got {prompt_tokens_est}")
    _require_int(max_output_tokens, "max_output_tokens")
    if max_output_tokens <= 0:
        raise ValueError(f"max_output_tokens must be > 0, got {max_output_tokens!r}")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        raise ValueError(f"duration_seconds must be a number, got {duration_seconds!r}")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError(f"duration_seconds must be finite and > 0, got {duration_seconds!r}")

    fitted = fit_long_edge(frame_width, frame_height, policy.max_edge)
    tokens_per_image = estimate_image_tokens(fitted[0], fitted[1])

    usable = int(context_size * policy.target_context_ratio)
    budget = usable - prompt_tokens_est - max_output_tokens
    if budget < policy.min_frames * tokens_per_image:
        raise FramePlanImpossible(
            "frame plan impossible: budget_tokens="
            f"{budget} < min_frames*tokens_per_image={policy.min_frames * tokens_per_image} "
            f"(min_frames={policy.min_frames}, tokens_per_image={tokens_per_image}, "
            f"context_size={context_size}, target_context_ratio={policy.target_context_ratio}, "
            f"prompt_tokens_est={prompt_tokens_est}, max_output_tokens={max_output_tokens}); "
            "remedies: raise context_size, lower max_output_tokens, or lower min_frames"
        )
    by_budget = budget // tokens_per_image

    frames = max(policy.min_frames, min(policy.max_frames, by_budget))
    duration_ceiling = max(1, math.ceil(duration_seconds * policy.probe_fps))
    frames = min(frames, duration_ceiling)

    if by_budget > policy.max_frames:
        clamp_reason = "max"
    elif duration_ceiling < by_budget:
        # here by_budget is within [min_frames, max_frames], so the
        # pre-duration frame count is by_budget itself
        clamp_reason = "duration"
    else:
        clamp_reason = "budget"

    timestamps = tuple(i * duration_seconds / frames for i in range(frames))
    return FramePlan(
        frames=frames,
        timestamps=timestamps,
        tokens_per_image=tokens_per_image,
        budget_tokens=budget,
        clamp_reason=clamp_reason,
        duration_source=duration_source,
    )


def conservative_tokens_per_image(*, max_edge: int) -> int:
    """1440p 16:9 box upper-bound token cost of one sampled frame.

    The sampler supports sources up to the ``max_edge x (max_edge * 9 // 16)``
    box and does **not** scale inside it (sources above the box are rejected
    by default; only an explicit ``max_edge < 2560`` opts into downscaling).
    The largest frame any plan can therefore receive is the 16:9 box itself,
    so the per-image token cost a plan may charge **without probing the
    source resolution** is

    ``tok/img = (max_edge // 32) * ((max_edge * 9 // 16) // 32)``

    — 3600 at the default ``max_edge = 2560``. Cheaper frames (smaller
    sources, or a 16:9-tilted box with a lower ``max_edge``) only leave
    budget headroom, they never overspend.

    Supersedes the earlier aspect-agnostic square worst case
    (``(max_edge // 32) ** 2`` with a 640 default): the square bound
    under-charged wide boxes and the 640 default was too small for sources
    the sampler now passes through unscaled.
    """
    if isinstance(max_edge, bool) or not isinstance(max_edge, int):
        raise ValueError(f"max_edge must be an int, got {max_edge!r}")
    if max_edge < TILE_EDGE:
        raise ValueError(f"max_edge must be >= {TILE_EDGE}, got {max_edge}")
    return max(1, (max_edge // TILE_EDGE) * ((max_edge * 9 // 16) // TILE_EDGE))


def estimate_prompt_tokens(*texts: str) -> int:
    """Conservative prompt-token estimate for the texts of one vision call.

    Returns ``max(800, sum(len(text) for text in texts))``. For CJK-heavy
    prompts one source character is one (or occasionally two) tokenizer
    tokens, so the character count is a workable upper bound for the prompt
    contribution; the 800-token floor keeps the estimate meaningful (and
    strictly positive) for empty or tiny prompts. Never returns 0.
    """
    for text in texts:
        if not isinstance(text, str):
            raise ValueError(f"prompt text must be a str, got {type(text).__name__}")
    return max(800, sum(len(text) for text in texts))


__all__ = [
    "FramePlan",
    "FramePlanImpossible",
    "compute_frame_plan",
    "conservative_tokens_per_image",
    "estimate_prompt_tokens",
]
