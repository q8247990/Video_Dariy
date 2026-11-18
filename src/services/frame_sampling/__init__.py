"""Frame-sampling stage: value types, policy, frame plan and fingerprints.

``types``
    Frozen value objects (:class:`FrameSample`, :class:`FrameSet`,
    :class:`SampleRequest`), the valid ``source``
    values and the :class:`FrameSampler` protocol.
``policy``
    :class:`FramePolicy` — the resolved per-model sampling policy.
``mode``
    :func:`resolve_input_mode` with the :data:`INPUT_MODE_IMAGE` /
    :data:`INPUT_MODE_VIDEO` constants — the provider's vision input mode
    (image frame sampling vs raw video).
``budget``
    :class:`FramePlan` / :class:`FramePlanImpossible` and
    :func:`compute_frame_plan` — the token-budget frame-count derivation.
``fingerprint``
    :func:`content_signature` (light size+head+tail file digest) and
    :func:`frame_set_fingerprint` (frame-set + policy + source fingerprint).
"""

from __future__ import annotations

from src.services.frame_sampling.budget import (
    FramePlan,
    FramePlanImpossible,
    compute_frame_plan,
    conservative_tokens_per_image,
    estimate_prompt_tokens,
)
from src.services.frame_sampling.fingerprint import content_signature, frame_set_fingerprint
from src.services.frame_sampling.mode import INPUT_MODE_IMAGE, INPUT_MODE_VIDEO, resolve_input_mode
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import (
    FRAME_SET_SOURCES,
    FrameSample,
    FrameSampler,
    FrameSet,
    SampleRequest,
)

__all__ = [
    "FRAME_SET_SOURCES",
    "FramePlan",
    "FramePlanImpossible",
    "FramePolicy",
    "FrameSample",
    "FrameSampler",
    "FrameSet",
    "INPUT_MODE_IMAGE",
    "INPUT_MODE_VIDEO",
    "SampleRequest",
    "compute_frame_plan",
    "conservative_tokens_per_image",
    "content_signature",
    "estimate_prompt_tokens",
    "frame_set_fingerprint",
    "resolve_input_mode",
]
