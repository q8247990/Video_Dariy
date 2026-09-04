"""Daily-summary generation pipeline stages.

This package owns the staged decomposition of the daily-summary
Celery task. The Celery task in :mod:`src.tasks.summarizer` is a
thin wrapper that opens a :func:`task_db_session`, builds the
composition root ``Container`` and delegates the heavy lifting to
the helpers in this module. Each module below owns one execution
stage:

``constants``
    Module-wide tunables (``SERIAL_SPLIT_PROMPT_THRESHOLD``,
    ``WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED``,
    ``DISPATCH_GUARD_STATE_KEY_PREFIX``).

``helpers``
    Shared text utilities (``clean_generated_text``,
    ``split_sentences``, ``trim_sentences_to_chars``,
    ``estimate_detail_length``, ``truncate_text``,
    ``build_fallback_overall_summary``,
    ``build_subject_fallback_summary``,
    ``complete_subject_sections``, ``normalise_attention_items``).
    Pure functions — no DB / no gateway.

``parsing``
    Parsers for the three LLM output shapes the prompt builders
    produce (``parse_summary_with_retry``,
    ``parse_subject_summary_output``, ``parse_rollup_output``,
    ``extract_json_payload``).

``generation``
    The LLM-facing helpers: ``generate_single_pass_summary_payload``
    (one prompt → one LLM call → summary payload) and
    ``generate_serial_summary_payload`` (one prompt per subject →
    one rollup prompt → summary payload). Token usage is recorded
    via the helper ``_record_token`` which calls
    :func:`record_token_usage` from :mod:`src.services.llm_qos`.

``normalization``
    Output clamp / structural normalisation
    (``clamp_summary_payload``) — keeps the final
    ``overall + detail`` under 500-900 chars while preserving whole
    sentences.

``evidence``
    The daily input preparation: ``Evidence`` dataclass +
    ``build_evidence`` that produces the half-open event range,
    home context, subject sections, missing subjects and attention
    candidates used by both the single and serial prompt builders.
    The prompt builder + the prompt-input dataclass are injected by
    the caller so this module stays free of
    :mod:`src.application.*` imports.

``lifecycle``
    Attempt lifecycle management: ``claim_attempt`` (per-date
    active slot), ``mark_running``, ``mark_failed``,
    ``mark_cancelled`` and the ``make_repository`` factory. The
    repository itself is supplied by the caller (it lives in
    :mod:`src.application.summary_attempt`).

``schedule``
    Dispatch-time guard: home-timezone day boundary parsing,
    schedule-text validation and the per-date ``AppRuntimeState``
    lock that prevents double-dispatch.

``finalize``
    The final-stage helpers the Celery task calls into:
    ``find_subscribed_webhooks`` (the per-day pre-publish discovery
    of subscribers).
    The actual ``publish_daily_summary`` call lives in
    :mod:`src.tasks.summarizer` so this layer stays free of
    :mod:`src.application.*` imports.

The stage modules never reach into :mod:`src.application.*` for
use cases / orchestrators / schemas. The Celery task is the only
place that imports :mod:`src.application.summary_attempt`,
:mod:`src.application.summary_publication`,
:mod:`src.application.pipeline` and the
:mod:`src.application.prompt` contracts / compiler; the services
layer stays dependency-free of those modules.
"""

from __future__ import annotations

from src.services.summarizer.constants import (
    DISPATCH_GUARD_STATE_KEY_PREFIX,
    SERIAL_SPLIT_PROMPT_THRESHOLD,
    WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
)
from src.services.summarizer.evidence import Evidence, build_evidence
from src.services.summarizer.finalize import (
    find_subscribed_webhooks,
)
from src.services.summarizer.generation import (
    generate_serial_summary_payload,
    generate_single_pass_summary_payload,
)
from src.services.summarizer.helpers import (
    build_fallback_overall_summary,
    build_subject_fallback_summary,
    clean_generated_text,
    complete_subject_sections,
    estimate_detail_length,
    normalise_attention_items,
    split_sentences,
    trim_sentences_to_chars,
    truncate_text,
)
from src.services.summarizer.lifecycle import (
    attempt_already_running,
    claim_attempt,
    mark_cancelled,
    mark_failed,
    mark_running,
)
from src.services.summarizer.normalization import clamp_summary_payload
from src.services.summarizer.parsing import (
    extract_json_payload,
    parse_rollup_output,
    parse_subject_summary_output,
    parse_summary_with_retry,
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
    "DISPATCH_GUARD_STATE_KEY_PREFIX",
    "Evidence",
    "SERIAL_SPLIT_PROMPT_THRESHOLD",
    "WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED",
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
    "parse_summary_with_retry",
    "release_dispatch_guard",
    "resolve_target_date",
    "scheduled_local_datetime",
    "split_sentences",
    "trim_sentences_to_chars",
    "truncate_text",
]
