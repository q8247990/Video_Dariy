"""Provider vision input-mode resolution (image frame sampling vs raw video).

The mode is derived from the same ``frame_sampling`` sub-dict of
``LLMProvider.extra_config_json`` that carries the sampling policy
(:mod:`src.services.frame_sampling.policy`), so one config block controls
both *how* frames are sampled and *whether* the vision call receives images
at all:

.. code-block:: json

    {"frame_sampling": {}}            -> image mode (sampling enabled)
    {"frame_sampling": {"enabled": false}}  -> video mode (today's raw-mp4 path)

Resolution is fully tolerant (same style as
:mod:`src.services.provider_generation_limits`): a missing or malformed
config never raises and never changes the caller's existing behaviour —
it simply resolves to the ``video`` mode. Only the two genuinely malformed
shapes (a ``frame_sampling`` that is not a dict, and a non-strict-bool
``enabled``) are logged as warnings.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from src.services.frame_sampling.policy import FRAME_SAMPLING_KEY

logger = logging.getLogger(__name__)

INPUT_MODE_IMAGE: Final = "image"
INPUT_MODE_VIDEO: Final = "video"


def resolve_input_mode(provider: Any) -> str:
    """Resolve the vision input mode for ``provider``.

    Reads ``getattr(provider, "extra_config_json", None)`` and returns
    :data:`INPUT_MODE_VIDEO` unless the provider has an explicit
    ``frame_sampling`` dict (``enabled`` absent or ``true``) — then
    :data:`INPUT_MODE_IMAGE``. Never raises on input values; malformed
    shapes are logged as warnings and fall back to ``video``.
    """
    extra = getattr(provider, "extra_config_json", None)
    if not isinstance(extra, dict):
        return INPUT_MODE_VIDEO

    raw_config = extra.get(FRAME_SAMPLING_KEY)
    if raw_config is None:
        return INPUT_MODE_VIDEO
    if not isinstance(raw_config, dict):
        logger.warning(
            "frame_sampling config %r is not a dict; using %s input mode",
            raw_config,
            INPUT_MODE_VIDEO,
        )
        return INPUT_MODE_VIDEO

    if "enabled" not in raw_config:
        return INPUT_MODE_IMAGE
    enabled = raw_config["enabled"]
    if enabled is True:
        return INPUT_MODE_IMAGE
    if enabled is False:
        return INPUT_MODE_VIDEO
    logger.warning(
        "frame_sampling.enabled=%r is not a bool; using %s input mode",
        enabled,
        INPUT_MODE_VIDEO,
    )
    return INPUT_MODE_VIDEO


__all__ = [
    "INPUT_MODE_IMAGE",
    "INPUT_MODE_VIDEO",
    "resolve_input_mode",
]
