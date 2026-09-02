"""Module-wide constants shared across the summarizer stages.

Centralised so the dispatch / split thresholds and the
``WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED`` event string can move in
lockstep with the logic that consults them. ``WEBHOOK_EVENT_*`` is
part of the public product contract (it is what subscribers see in
their ``event`` filter); renaming it requires a migration of every
deployed subscription.
"""

from __future__ import annotations

#: Event string the daily-summary webhook payload uses. Mirrors the
#: constant in :mod:`src.application.summary_publication.use_case` so
#: the publisher's subscription filter recognises it.
WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED: str = "daily_summary_generated"

#: Prefix for the ``AppRuntimeState`` key that gates per-date
#: dispatch. Full key is ``f"{prefix}:{target_date.isoformat()}"``.
DISPATCH_GUARD_STATE_KEY_PREFIX: str = "daily_summary_dispatch_guard"

#: Character-count threshold above which the summarizer switches from
#: the single-pass prompt to the serial (per-subject + rollup)
#: generation path. Set conservatively so the single-pass LLM call
#: stays inside the model's context window for the typical family
#: day.
SERIAL_SPLIT_PROMPT_THRESHOLD: int = 28000


__all__ = [
    "DISPATCH_GUARD_STATE_KEY_PREFIX",
    "SERIAL_SPLIT_PROMPT_THRESHOLD",
    "WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED",
]
