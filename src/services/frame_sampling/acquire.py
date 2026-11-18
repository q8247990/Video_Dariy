"""Production entry point for frame acquisition (image-sampling path).

``acquire_frame_set`` is the single callable the analysis port wires in
for the image-input path:

1. resolve the provider's vision input mode — a video-mode provider
   raises :class:`SamplerDisabled` before any I/O (no fallback, no
   fabricated frames);
2. resolve the per-model sampling policy;
3. consult the on-disk
   :class:`~src.services.frame_sampling.cache.FrameSetCache` (root =
   ``settings.PLAYBACK_CACHE_ROOT``): a hit returns the cached
   :class:`FrameSet` (``source="cache"``) with identical frames /
   timestamps / candidates, and the sampler is never invoked;
4. on a miss, delegate to
   :func:`src.infrastructure.frame_sampler.client.build_frame_set`,
   cache the result, and return it (``source="sampler"``).

There is no degradation chain: :class:`SamplerUnavailable` propagates
unchanged (nothing is cached on failure), and a broken cache root
surfaces as :class:`OSError` rather than a silent miss.

Architecture note: this module is the frame-sampling port's production
binding. ``tests/architecture/test_dependency_boundaries.py`` carves out
exactly this file from the ``services -> infrastructure`` rule (the
plan-mandated entry point must be callable with only
``(provider, video_path, plan, db)`` — the sampler client binding is its
core responsibility, the same shape as the composition-root binding
carved out of the application rule for ``src/application/bootstrap.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from src.core.config import settings
from src.infrastructure.frame_sampler.client import SamplerDisabled, build_frame_set
from src.services.frame_sampling.budget import FramePlan
from src.services.frame_sampling.cache import FrameSetCache
from src.services.frame_sampling.mode import INPUT_MODE_IMAGE, resolve_input_mode
from src.services.frame_sampling.policy import parse_frame_policy
from src.services.frame_sampling.types import FrameSet

logger = logging.getLogger(__name__)


def acquire_frame_set(
    provider: Any,
    video_path: str,
    plan: FramePlan,
    *,
    db: Session | None = None,
) -> FrameSet:
    """Acquire the :class:`FrameSet` for one analysis sub-chunk (cache-first).

    Args:
        provider: the ``LLMProvider`` (only ``extra_config_json`` is read).
        video_path: absolute path of the sub-chunk's video file.
        plan: the budgeted :class:`FramePlan` for this call.
        db: an optional DB session for the sampler settings lookup
            (``build_frame_set`` opens a fresh ``task_db_session`` when
            ``None``).

    Returns:
        The cached :class:`FrameSet` (``source="cache"``) on a hit, or the
        freshly sampled one (``source="sampler"``) on a miss — in both
        cases with identical frames / timestamps / candidates.

    Raises:
        SamplerDisabled: the provider is in video input mode.
        SamplerUnavailable: the sampler could not be reached / returned —
            propagated unchanged (no fallback; nothing is cached).
        OSError: the cache root is unusable (exists as a file, unwritable)
            — surfaced loudly, never a silent success.
    """
    if resolve_input_mode(provider) != INPUT_MODE_IMAGE:
        raise SamplerDisabled("provider is in video input mode")
    policy = parse_frame_policy(provider)

    cache = FrameSetCache(Path(settings.PLAYBACK_CACHE_ROOT))
    cached = cache.get(video_path=video_path, policy=policy, plan=plan)
    if cached is not None:
        logger.info(
            "frame cache hit for %s (policy_key=%s, %d frames)",
            video_path,
            cache.policy_key(policy, plan)[:12],
            len(cached.frames),
        )
        return cached

    frame_set = build_frame_set(provider, video_path, plan, db=db)
    cache.put(video_path=video_path, policy=policy, plan=plan, frame_set=frame_set)
    logger.info(
        "frame cache populated for %s (policy_key=%s, %d frames)",
        video_path,
        cache.policy_key(policy, plan)[:12],
        len(frame_set.frames),
    )
    return frame_set


__all__ = ["acquire_frame_set"]
