"""Daily-summary pipeline stages.

``contracts``
    Tunables and webhook event names.
``prepare``
    Text helpers, evidence building and the :class:`Evidence` bundle.
``generation``
    Output parsers, clamp/normalisation, and the single/serial payload builders.
``finalize``
    Attempt lifecycle transitions and the publication step.
``schedule``
    Home timezone, daily trigger parsing and the dispatch guard.
"""

from __future__ import annotations

from src.services.summarizer.contracts import (
    SERIAL_SPLIT_PROMPT_THRESHOLD,
    WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
)
from src.services.summarizer.finalize import (
    attempt_already_running,
    claim_attempt,
    find_subscribed_webhooks,
    mark_cancelled,
    mark_failed,
    mark_running,
)
from src.services.summarizer.generation import (
    clamp_summary_payload,
    extract_json_payload,
    generate_serial_summary_payload,
    generate_single_pass_summary_payload,
    parse_rollup_output,
    parse_subject_summary_output,
)
from src.services.summarizer.prepare import (
    Evidence,
    PromptBuilder,
    build_evidence,
    build_fallback_overall_summary,
    build_subject_fallback_summary,
    clean_generated_text,
    complete_subject_sections,
    estimate_detail_length,
    normalise_attention_items,
    split_sentences,
    trim_sentences_to_chars,
    truncate_for_summary,
)
from src.services.summarizer.schedule import (
    claim_dispatch_guard,
    get_daily_schedule,
    get_home_timezone,
    has_existing_summary_or_task,
    parse_schedule_time,
    release_dispatch_guard,
    resolve_target_date,
    scheduled_local_datetime,
)

__all__ = [
    "SERIAL_SPLIT_PROMPT_THRESHOLD",
    "WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED",
    "Evidence",
    "PromptBuilder",
    "attempt_already_running",
    "build_evidence",
    "build_fallback_overall_summary",
    "build_subject_fallback_summary",
    "claim_attempt",
    "claim_dispatch_guard",
    "clamp_summary_payload",
    "clean_generated_text",
    "complete_subject_sections",
    "estimate_detail_length",
    "extract_json_payload",
    "find_subscribed_webhooks",
    "generate_serial_summary_payload",
    "generate_single_pass_summary_payload",
    "get_daily_schedule",
    "get_home_timezone",
    "has_existing_summary_or_task",
    "mark_cancelled",
    "mark_failed",
    "mark_running",
    "normalise_attention_items",
    "parse_rollup_output",
    "parse_schedule_time",
    "parse_subject_summary_output",
    "release_dispatch_guard",
    "resolve_target_date",
    "scheduled_local_datetime",
    "split_sentences",
    "trim_sentences_to_chars",
    "truncate_for_summary",
]
