"""Per-model frame-sampling policy parsing and clamping.

The policy lives under the ``frame_sampling`` sub-dict of
``LLMProvider.extra_config_json`` so it can be edited from the provider form
without a schema migration:

.. code-block:: json

    {"frame_sampling": {"mode": "dedupe", "max_edge": 960, "min_frames": 8}}

The ``mode`` key selects the sampling strategy (``uniform`` / ``dedupe`` /
``event``); the per-mode numeric bundles (:data:`PRESETS`) only supply
*defaults* for the numeric keys — an explicit key always wins, and a key
missing from both falls back to the built-in default. The legacy ``preset``
key (``baseline`` / ``dedupe`` / ``event``) is still accepted and mapped to
a mode (``baseline`` -> ``uniform``) with a one-line warning; an explicit
``mode`` wins over it. Parsing is fully tolerant (same style as
:mod:`src.services.provider_generation_limits`): malformed values are never
fatal — they are logged as a warning and replaced by the next fallback layer.
A clamped/ignored value is always logged via ``logging``.

Numeric bounds (clamped; ``max_edge`` additionally snapped down to a
multiple of 32) and their built-in defaults:

- ``mode``: ``uniform`` / ``dedupe`` / ``event`` (default ``event``)
- ``target_context_ratio``: ``[0.5, 1.0]`` (default ``0.9``)
- ``max_edge``: ``[320, 2560]`` (default ``2560``)
- ``probe_fps``: ``[0.25, 10.0]`` (default ``2.0``)
- ``min_frames``: ``[1, 60]`` (default ``4``)
- ``max_frames``: ``[1, 600]`` (default ``120``)
- ``jpeg_quality``: ``[60, 95]`` (default ``85``)
- ``dedupe_phash_distance``: ``[1, 32]``, ``None`` = no dedupe (default ``None``)
- ``window_pre_seconds``: ``[0.0, 60.0]`` (default ``5.0``)
- ``window_post_seconds``: ``[0.0, 120.0]`` (default ``10.0``)
- ``max_event_windows``: ``[1, 10]`` (default ``4``)
- ``max_output_tokens``: positive int (default ``2048``)
- ``enabled``: strict bool (default ``True``)

``max_edge`` semantics (ADR 0014): the sampler supports sources up to the
``2560 x 1440`` box and does **not** scale inside it — a source at or below
the box passes through unchanged, and a larger source is rejected by default
(opt-in downscale only when ``max_edge`` is set below 2560). The built-in
default of 2560 is therefore the box upper bound; the token budget charges
the 16:9 box at that edge (3600 tokens at the default, see
:func:`src.services.frame_sampling.budget.conservative_tokens_per_image`).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Final, Literal

logger = logging.getLogger(__name__)

FRAME_SAMPLING_KEY = "frame_sampling"

SamplingMode = Literal["uniform", "dedupe", "event"]

DEFAULT_MODE: Final[SamplingMode] = "event"

#: Numeric default bundles keyed by sampling mode. ``uniform`` and ``event``
#: legitimately share the same numeric defaults (the built-in values *are*
#: the event defaults; ``uniform`` is the reference point matching the legacy
#: raw-mp4 sampling behavior).
PRESETS: Final[dict[str, dict[str, object]]] = {
    "uniform": {},
    # pHash dedupe enabled, lower probe rate and smaller frame budget.
    "dedupe": {"dedupe_phash_distance": 6, "probe_fps": 1.0, "max_frames": 60},
    "event": {},
}

#: Legacy ``preset`` key values mapped to their sampling mode.
_LEGACY_PRESET_TO_MODE: Final[dict[str, SamplingMode]] = {
    "baseline": "uniform",
    "dedupe": "dedupe",
    "event": "event",
}

MAX_EDGE_ALIGNMENT = 32

PickFn = Callable[..., Any]


@dataclass(frozen=True)
class FramePolicy:
    """Resolved per-model frame-sampling policy (all values within safe ranges)."""

    mode: SamplingMode = "event"
    target_context_ratio: float = 0.9
    max_edge: int = 2560
    probe_fps: float = 2.0
    min_frames: int = 4
    max_frames: int = 120
    jpeg_quality: int = 85
    dedupe_phash_distance: int | None = None
    window_pre_seconds: float = 5.0
    window_post_seconds: float = 10.0
    max_event_windows: int = 4
    max_output_tokens: int = 2048
    enabled: bool = True


_BUILTIN: FramePolicy = FramePolicy()


def _coerce_bool(value: Any) -> bool | None:
    """Parse a strict bool from JSON; anything else (incl. ``1``) is rejected."""
    if isinstance(value, bool):
        return value
    return None


def _coerce_int(value: Any) -> int | None:
    """Parse an int from JSON; ignore bools / garbage / non-integral floats."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def _coerce_float(value: Any) -> float | None:
    """Parse a finite float from JSON; ignore bools / garbage / NaN / inf."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _bounded_int(key: str, raw: Any, *, low: int, high: int, default: int) -> int:
    """Clamp a raw JSON value into ``[low, high]`` (malformed → default)."""
    parsed = _coerce_int(raw)
    if parsed is None:
        logger.warning(
            "frame_sampling.%s=%r is not an int in [%d, %d]; using default %d",
            key,
            raw,
            low,
            high,
            default,
        )
        return default
    if low <= parsed <= high:
        return parsed
    clamped = max(low, min(high, parsed))
    logger.warning(
        "frame_sampling.%s=%d out of range [%d, %d]; clamped to %d",
        key,
        parsed,
        low,
        high,
        clamped,
    )
    return clamped


def _bounded_float(key: str, raw: Any, *, low: float, high: float, default: float) -> float:
    """Clamp a raw JSON value into ``[low, high]`` (malformed → default)."""
    parsed = _coerce_float(raw)
    if parsed is None:
        logger.warning(
            "frame_sampling.%s=%r is not a finite float in [%s, %s]; using default %s",
            key,
            raw,
            low,
            high,
            default,
        )
        return default
    if low <= parsed <= high:
        return float(parsed)
    clamped = max(low, min(high, parsed))
    logger.warning(
        "frame_sampling.%s=%s out of range [%s, %s]; clamped to %s",
        key,
        parsed,
        low,
        high,
        clamped,
    )
    return float(clamped)


def _resolve_mode(config: dict[str, Any]) -> SamplingMode:
    """Resolved sampling mode (unknown / malformed → :data:`DEFAULT_MODE` + warning).

    An explicit ``mode`` key wins. The legacy ``preset`` key is still
    accepted (``baseline`` -> ``uniform``, ``dedupe`` -> ``dedupe``,
    ``event`` -> ``event``) with a one-line deprecation warning.
    """
    raw_mode = config.get("mode")
    if raw_mode is not None:
        if isinstance(raw_mode, str) and raw_mode in PRESETS:
            return raw_mode  # type: ignore[return-value]
        logger.warning(
            "frame_sampling.mode=%r is unknown or not a string; using mode %r",
            raw_mode,
            DEFAULT_MODE,
        )
        return DEFAULT_MODE
    raw_preset = config.get("preset")
    if raw_preset is not None:
        if isinstance(raw_preset, str) and raw_preset in _LEGACY_PRESET_TO_MODE:
            mode = _LEGACY_PRESET_TO_MODE[raw_preset]
            logger.warning(
                "frame_sampling.preset=%r is a legacy key; mapped to mode %r",
                raw_preset,
                mode,
            )
            return mode
        logger.warning(
            "frame_sampling.preset=%r is unknown or not a string; using mode %r",
            raw_preset,
            DEFAULT_MODE,
        )
        return DEFAULT_MODE
    return DEFAULT_MODE


def _resolve_max_edge(pick: PickFn) -> int:
    """``max_edge`` clamped to ``[320, 2560]`` and snapped down to a 32 multiple.

    2560 is the 1440p box upper bound (the sampler does not scale inside it;
    see the module docstring / ADR 0014).
    """
    max_edge = _bounded_int(
        "max_edge", pick("max_edge"), low=320, high=2560, default=_BUILTIN.max_edge
    )
    snapped = max_edge - (max_edge % MAX_EDGE_ALIGNMENT)
    if snapped != max_edge:
        logger.warning(
            "frame_sampling.max_edge=%d snapped down to a multiple of %d: %d",
            max_edge,
            MAX_EDGE_ALIGNMENT,
            snapped,
        )
        return snapped
    return max_edge


def _resolve_phash(pick: PickFn) -> int | None:
    """``dedupe_phash_distance`` (``None`` = no dedupe; else clamped to ``[1, 32]``)."""
    raw = pick("dedupe_phash_distance", none_allowed=True)
    if raw is None:
        return None
    parsed = _coerce_int(raw)
    if parsed is None:
        logger.warning(
            "frame_sampling.dedupe_phash_distance=%r is not an int in [1, 32]; "
            "ignoring (no dedupe)",
            raw,
        )
        return None
    if not 1 <= parsed <= 32:
        clamped = max(1, min(32, parsed))
        logger.warning(
            "frame_sampling.dedupe_phash_distance=%d out of range [1, 32]; clamped to %d",
            parsed,
            clamped,
        )
        return clamped
    return parsed


def _resolve_max_output_tokens(pick: PickFn) -> int:
    """Positive ``max_output_tokens`` (malformed / non-positive → default)."""
    raw = pick("max_output_tokens")
    parsed = _coerce_int(raw)
    if parsed is None or parsed <= 0:
        if parsed is None:
            logger.warning(
                "frame_sampling.max_output_tokens=%r is not a positive int; using default %d",
                raw,
                _BUILTIN.max_output_tokens,
            )
        else:
            logger.warning(
                "frame_sampling.max_output_tokens=%d is not positive; using default %d",
                parsed,
                _BUILTIN.max_output_tokens,
            )
        return _BUILTIN.max_output_tokens
    return parsed


def _resolve_enabled(pick: PickFn) -> bool:
    """Strict-bool ``enabled`` (malformed → default ``True``)."""
    raw = pick("enabled")
    parsed = _coerce_bool(raw)
    if parsed is None:
        logger.warning("frame_sampling.enabled=%r is not a bool; using default True", raw)
        return True
    return parsed


def _resolve_frame_bounds(pick: PickFn) -> tuple[int, int]:
    """Clamped ``(min_frames, max_frames)`` with ``min_frames <= max_frames``."""
    min_frames = _bounded_int(
        "min_frames", pick("min_frames"), low=1, high=60, default=_BUILTIN.min_frames
    )
    max_frames = _bounded_int(
        "max_frames", pick("max_frames"), low=1, high=600, default=_BUILTIN.max_frames
    )
    if min_frames > max_frames:
        logger.warning(
            "frame_sampling.min_frames=%d > max_frames=%d; clamped min_frames down to %d",
            min_frames,
            max_frames,
            max_frames,
        )
        return max_frames, max_frames
    return min_frames, max_frames


def parse_frame_policy(provider: Any) -> FramePolicy:
    """Resolve the frame-sampling policy for ``provider``.

    Reads ``getattr(provider, "extra_config_json", None)``; tolerates
    ``None``/non-dict JSON (full built-in defaults). Never raises on input
    values — clamped/ignored values are logged as warnings. The sampling
    mode comes from the ``mode`` key (the legacy ``preset`` key is mapped
    to a mode with a warning; an explicit ``mode`` wins).
    """
    extra = getattr(provider, "extra_config_json", None)
    if not isinstance(extra, dict):
        return FramePolicy()

    raw_config = extra.get(FRAME_SAMPLING_KEY)
    if raw_config is None:
        config: dict[str, Any] = {}
    elif isinstance(raw_config, dict):
        config = raw_config
    else:
        logger.warning("frame_sampling config %r is not a dict; using full defaults", raw_config)
        config = {}

    mode = _resolve_mode(config)
    mode_bundle = PRESETS[mode]

    def pick(key: str, *, none_allowed: bool = False) -> Any:
        """Explicit config value > mode bundle default > built-in default."""
        if key in config:
            raw = config[key]
            if raw is not None or none_allowed:
                return raw
        if key in mode_bundle:
            return mode_bundle[key]
        return getattr(_BUILTIN, key)

    min_frames, max_frames = _resolve_frame_bounds(pick)

    return FramePolicy(
        mode=mode,
        target_context_ratio=_bounded_float(
            "target_context_ratio",
            pick("target_context_ratio"),
            low=0.5,
            high=1.0,
            default=_BUILTIN.target_context_ratio,
        ),
        max_edge=_resolve_max_edge(pick),
        probe_fps=_bounded_float(
            "probe_fps",
            pick("probe_fps"),
            low=0.25,
            high=10.0,
            default=_BUILTIN.probe_fps,
        ),
        min_frames=min_frames,
        max_frames=max_frames,
        jpeg_quality=_bounded_int(
            "jpeg_quality",
            pick("jpeg_quality"),
            low=60,
            high=95,
            default=_BUILTIN.jpeg_quality,
        ),
        dedupe_phash_distance=_resolve_phash(pick),
        window_pre_seconds=_bounded_float(
            "window_pre_seconds",
            pick("window_pre_seconds"),
            low=0.0,
            high=60.0,
            default=_BUILTIN.window_pre_seconds,
        ),
        window_post_seconds=_bounded_float(
            "window_post_seconds",
            pick("window_post_seconds"),
            low=0.0,
            high=120.0,
            default=_BUILTIN.window_post_seconds,
        ),
        max_event_windows=_bounded_int(
            "max_event_windows",
            pick("max_event_windows"),
            low=1,
            high=10,
            default=_BUILTIN.max_event_windows,
        ),
        max_output_tokens=_resolve_max_output_tokens(pick),
        enabled=_resolve_enabled(pick),
    )


__all__ = [
    "DEFAULT_MODE",
    "FRAME_SAMPLING_KEY",
    "FramePolicy",
    "PRESETS",
    "parse_frame_policy",
]
