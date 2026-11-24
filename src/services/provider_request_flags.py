"""Per-provider request-body flags shared by one-shot LLM call sites.

Strata maps the body field ``strata_checkpoint: false`` (engine key
``ckpt=0``) to "do not store a checkpoint or a resumable live session for
this call". One-shot calls (file-level vision analysis, daily summary)
should send it so they stop parking large conversation snapshots; calls
that keep asking in the same conversation (QA) must not.

The flag is opt-in per provider: OpenAI-compatible servers such as vLLM
reject unknown body fields, so it is emitted only when the provider row
declares ``extra_config_json["strata_checkpoint"] is False``.
"""

from __future__ import annotations

from typing import Any

KEY_STRATA_CHECKPOINT = "strata_checkpoint"


def resolve_one_shot_request_extras(provider: Any) -> dict[str, Any]:
    """Request-body extras for a call nobody will resume.

    Returns ``{"strata_checkpoint": False}`` when the provider opts in via
    ``extra_config_json["strata_checkpoint"] is False``; an empty dict
    otherwise (the default, safe for providers that reject the field).
    """
    extra = getattr(provider, "extra_config_json", None)
    if isinstance(extra, dict) and extra.get(KEY_STRATA_CHECKPOINT) is False:
        return {KEY_STRATA_CHECKPOINT: False}
    return {}


__all__ = ["KEY_STRATA_CHECKPOINT", "resolve_one_shot_request_extras"]
