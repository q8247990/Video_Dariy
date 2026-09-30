"""Provider-level generation limits (``max_model_len`` / ``max_output_tokens``).

Both keys live in :attr:`src.models.llm_provider.LLMProvider.extra_config_json`
so they can be edited from the provider form without a schema migration:

* ``max_model_len`` — total context window (input + output).
* ``max_output_tokens`` — per-call output cap.

Both are optional and backward-compatible: when unset the caller keeps its own
scene default (the analyzer / summarizer pass ``8192``; QA stays uncapped; the
entity-appearance vision call keeps ``300``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

KEY_MAX_MODEL_LEN = "max_model_len"
KEY_MAX_OUTPUT_TOKENS = "max_output_tokens"
KEY_NUM_FRAMES = "num_frames"

NUM_FRAMES_MIN = 1
NUM_FRAMES_MAX = 600


@dataclass(frozen=True)
class GenerationLimits:
    """Resolved provider generation limits (``None`` = not configured)."""

    max_model_len: Optional[int] = None
    max_output_tokens: Optional[int] = None
    num_frames: Optional[int] = None

    def output_tokens(self, *, scene_default: Optional[int]) -> Optional[int]:
        """Configured output cap, clamped to ``max_model_len`` when both set."""
        if self.max_output_tokens is None:
            return scene_default
        if self.max_model_len is not None:
            return min(self.max_output_tokens, self.max_model_len)
        return self.max_output_tokens

    def input_tokens(self, *, scene_default: Optional[int]) -> Optional[int]:
        """Remaining input budget (``max_model_len`` minus the output cap).

        ``None`` when ``max_model_len`` is unset. Used for pre-call validation
        and as the future basis for frame-count reduction.
        """
        if self.max_model_len is None:
            return None
        output = self.output_tokens(scene_default=scene_default) or 0
        return max(self.max_model_len - output, 0)


def _positive_int(value: Any) -> Optional[int]:
    """Parse a positive int from JSON; ignore bools / garbage / non-positives."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            parsed = int(text)
            return parsed if parsed > 0 else None
    return None


def _bounded_int(value: Any, low: int, high: int) -> Optional[int]:
    """Parse a positive int and clamp it into ``[low, high]``."""
    parsed = _positive_int(value)
    if parsed is None:
        return None
    return max(low, min(high, parsed))


def get_generation_limits(provider: Any) -> GenerationLimits:
    """Read the limits off a provider row (tolerates ``None``/non-dict JSON)."""
    extra = getattr(provider, "extra_config_json", None)
    if not isinstance(extra, dict):
        return GenerationLimits()
    return GenerationLimits(
        max_model_len=_positive_int(extra.get(KEY_MAX_MODEL_LEN)),
        max_output_tokens=_positive_int(extra.get(KEY_MAX_OUTPUT_TOKENS)),
        num_frames=_bounded_int(extra.get(KEY_NUM_FRAMES), NUM_FRAMES_MIN, NUM_FRAMES_MAX),
    )


def resolve_max_output_tokens(provider: Any, *, scene_default: Optional[int]) -> Optional[int]:
    """The output cap to send for ``provider`` in a scene (``None`` = uncapped)."""
    return get_generation_limits(provider).output_tokens(scene_default=scene_default)


def resolve_num_frames(provider: Any, *, scene_default: int) -> int:
    """The video ``num_frames`` to sample for ``provider``.

    Configured ``num_frames`` (clamped to ``[NUM_FRAMES_MIN, NUM_FRAMES_MAX]``)
    when set, otherwise the caller's ``scene_default``.
    """
    configured = get_generation_limits(provider).num_frames
    return configured if configured is not None else scene_default


__all__ = [
    "GenerationLimits",
    "KEY_MAX_MODEL_LEN",
    "KEY_MAX_OUTPUT_TOKENS",
    "KEY_NUM_FRAMES",
    "NUM_FRAMES_MAX",
    "NUM_FRAMES_MIN",
    "get_generation_limits",
    "resolve_max_output_tokens",
    "resolve_num_frames",
]
