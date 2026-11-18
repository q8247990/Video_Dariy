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
``counters``
    Per-provider frame-sampling acquisition counters persisted on the
    ``/metrics`` surface (todo 12): :func:`record_frame_sampling_success`
    / :func:`record_frame_sampling_failure` (transactional increments on
    the ``AppRuntimeState`` snapshot),
    :func:`is_frame_acquisition_failure` (sampler-failure classification)
    and :func:`classify_failure_kind` (the
    ``TaskLog.detail_json["failure_kind"]`` marker).
"""

from __future__ import annotations

from src.services.frame_sampling.budget import (
    FramePlan,
    FramePlanImpossible,
    compute_frame_plan,
    conservative_tokens_per_image,
    estimate_prompt_tokens,
)
from src.services.frame_sampling.counters import (
    FAILURE_KIND_LLM,
    FAILURE_KIND_SAMPLER_UNAVAILABLE,
    classify_failure_kind,
    is_frame_acquisition_failure,
    record_frame_sampling_failure,
    record_frame_sampling_success,
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
    "FAILURE_KIND_LLM",
    "FAILURE_KIND_SAMPLER_UNAVAILABLE",
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
    "classify_failure_kind",
    "compute_frame_plan",
    "conservative_tokens_per_image",
    "content_signature",
    "estimate_prompt_tokens",
    "frame_set_fingerprint",
    "is_frame_acquisition_failure",
    "record_frame_sampling_failure",
    "record_frame_sampling_success",
    "resolve_input_mode",
]
