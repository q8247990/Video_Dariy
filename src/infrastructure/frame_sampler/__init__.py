"""Frame sampler service HTTP client (backend side of the sampling contract).

The frame sampler runs as a separate container; this package holds the
backend-side HTTP client that acquires a :class:`FrameSet` from it, plus the
production port function :func:`build_frame_set` that the analysis stage
wires in (plan todo 7).

Contract (frozen, implemented by the sampler service):

.. code-block:: http

    POST {frame_sampler_url}/sample

with the request/response payloads defined by
:mod:`src.services.frame_sampling` value types and
:mod:`src.services.frame_sampling.policy`.
"""

from __future__ import annotations

from src.infrastructure.frame_sampler.client import (
    DEFAULT_TIMEOUT_SECONDS,
    FrameSamplerClient,
    FrameSamplerSettings,
    SamplerDisabled,
    SamplerUnavailable,
    build_frame_set,
    load_frame_sampler_settings,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FrameSamplerClient",
    "FrameSamplerSettings",
    "SamplerDisabled",
    "SamplerUnavailable",
    "build_frame_set",
    "load_frame_sampler_settings",
]
