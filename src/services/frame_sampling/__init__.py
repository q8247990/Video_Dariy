"""Frame-sampling stage: value types, policy, frame plan and fingerprints.

``types``
    Frozen value objects (:class:`FrameSample`, :class:`EventCandidate`,
    :class:`FrameSet`, :class:`SampleRequest`), the valid ``source``
    values and the :class:`FrameSampler` protocol.
``policy``
    :class:`FramePolicy` — the resolved per-model sampling policy.
``budget``
    :class:`FramePlan` / :class:`FramePlanImpossible` and
    :func:`compute_frame_plan` — the token-budget frame-count derivation.
``fingerprint``
    :func:`content_signature` (light size+head+tail file digest) and
    :func:`frame_set_fingerprint` (frame-set + policy + source fingerprint).
"""

from __future__ import annotations

from src.services.frame_sampling.budget import FramePlan, FramePlanImpossible, compute_frame_plan
from src.services.frame_sampling.fingerprint import content_signature, frame_set_fingerprint
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import (
    FRAME_SET_SOURCES,
    EventCandidate,
    FrameSample,
    FrameSampler,
    FrameSet,
    SampleRequest,
)

__all__ = [
    "EventCandidate",
    "FRAME_SET_SOURCES",
    "FramePlan",
    "FramePlanImpossible",
    "FramePolicy",
    "FrameSample",
    "FrameSampler",
    "FrameSet",
    "SampleRequest",
    "compute_frame_plan",
    "content_signature",
    "frame_set_fingerprint",
]
