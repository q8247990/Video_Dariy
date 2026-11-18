"""Fail-fast frame-plan validation for image-mode LLM providers (Wave E / todo 25).

When a provider is created, updated or tested with an image ``frame_sampling``
config, the representative clip (see
:func:`src.services.llm_provider_tester.check_image_frame_plan`) must already
fit the model's ``max_model_len``: otherwise the first analysis run would
exceed the context window and fail with an opaque model-side error.

:func:`validate_image_frame_plan` is the single entry point used by the
provider use cases. It returns the computed :class:`FramePlan` for image-mode
configurations (``None`` for video mode, which is skipped entirely) and raises
:class:`~src.core.exceptions.DomainError` with an actionable message when the
plan cannot be built. The use cases map that exception to business error
``4001`` (HTTP 409) — the same code the other provider validation failures
use — so operators get a fail-fast, machine-readable rejection instead of a
poisoned provider row.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from src.core.exceptions import DomainError
from src.services.frame_sampling.budget import FramePlan
from src.services.frame_sampling.mode import INPUT_MODE_IMAGE, resolve_input_mode
from src.services.llm_provider_tester import check_image_frame_plan, frame_plan_error_message

logger = logging.getLogger(__name__)


def _as_provider(provider_data: Any) -> Any:
    """Normalise a raw dump dict into a provider-like object.

    The use cases hold a plain dict of incoming fields (create / update dump)
    before persistence; the plan helpers only need ``extra_config_json``, so a
    :class:`SimpleNamespace` stub is sufficient. Provider-like objects (ORM
    rows, test stubs) pass through unchanged.
    """
    if isinstance(provider_data, dict):
        return SimpleNamespace(extra_config_json=provider_data.get("extra_config_json"))
    return provider_data


def validate_image_frame_plan(provider_data: Any) -> FramePlan | None:
    """Validate the image-mode frame plan for ``provider_data`` pre-persistence.

    ``provider_data`` is either the incoming field dict (create / update dump)
    or a provider-like object exposing ``extra_config_json``.

    * Video-mode providers (no ``frame_sampling`` config) are skipped and
      ``None`` is returned — no error is ever raised for them.
    * Image-mode providers must carry a configured ``max_model_len``; a
      missing / zero value is a business error (a default context is never
      assumed).
    * The representative clip (60 s, a frame at the 1440p 16:9 box bound,
      800 prompt tokens) must fit the budget;
      :class:`~src.services.frame_sampling.budget.FramePlanImpossible`
      becomes a business error listing the computed numbers and exactly three
      remedies (raise ``max_model_len``, lower ``max_output_tokens``, lower
      ``min_frames``).
    * Any other ``ValueError`` from the plan is re-raised as a business error
      with the original message appended.
    """
    provider = _as_provider(provider_data)
    if resolve_input_mode(provider) != INPUT_MODE_IMAGE:
        return None
    try:
        return check_image_frame_plan(provider)
    except ValueError as exc:  # includes FramePlanImpossible
        logger.warning("image frame plan validation failed: %s", exc)
        raise DomainError(frame_plan_error_message(provider, exc)) from exc
