"""LLM generation helpers — single-pass and serial (per-subject + rollup).

Each helper takes the prepared :class:`~src.services.summarizer.evidence.Evidence`
plus a fresh ``LLMGatewayPort`` and produces the structured summary
payload (``overall_summary``, ``subject_sections``,
``attention_items``) and the bookkeeping details the Celery task
writes back to the ``task_log`` (``prompt_chars``, ``parse_retried``).

The gateway is *not* opened by these helpers — the caller (the
orchestrator) opens and closes it. This keeps the helpers easy to
unit-test (a stub gateway suffices) and matches the analyzer's
"no long-lived DB connection during LLM call" pattern: the caller
passes in a gateway built from the composition root
``Container.llm_factory``, the LLM HTTP call happens entirely
inside the helper, and the orchestrator closes the gateway after
the helper returns.

The two paths:

* :func:`generate_single_pass_summary_payload` — one
  ``chat_completion`` call with the full single-pass prompt. Falls
  back to a parse-retry on the same prompt; falls back to the
  stable empty-day text on the second parse failure.
* :func:`generate_serial_summary_payload` — one
  ``chat_completion`` call per subject + one for the rollup. The
  same parse-retry policy applies, plus a per-subject fallback text
  when a single subject fails.

Both helpers call :func:`record_token_usage` after every successful
``chat_completion`` so the ``LLMUsageLog`` row is fresh at the end.
The caller's ``db`` transaction is *not* held during the LLM call:
the token usage row is written in the *next* call-site transaction
(the orchestrator commits the token writes as part of the
post-LLM commit boundary).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from src.core.i18n.locale_directive import get_retry_json_instruction
from src.models.event_record import EventRecord
from src.services.daily_summary.output_parser import DailySummaryOutputError
from src.services.llm_qos import record_token_usage
from src.services.prompt_builder.compression.daily_summary_compressor import (
    compress_daily_input,
)
from src.services.prompt_builder.v2.daily_summary import (
    build_daily_rollup_prompt,
    build_subject_summary_prompt,
)
from src.services.summarizer.helpers import (
    build_fallback_overall_summary,
    build_subject_fallback_summary,
    clean_generated_text,
    complete_subject_sections,
)
from src.services.summarizer.parsing import (
    parse_daily_summary_output,
    parse_rollup_output,
    parse_subject_summary_output,
)

logger = logging.getLogger(__name__)


def _record_token(
    db: Any,
    *,
    client: Any,
    provider_id: int,
    provider_name_snapshot: Optional[str],
) -> None:
    """Write the gateway's last-usage row into ``LLMUsageLog``."""
    usage = client.get_last_usage()
    if usage is None:
        return
    record_token_usage(
        db,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
        scene="daily_summary",
        usage=usage,
    )


def _chat(
    db: Any,
    *,
    client: Any,
    provider_id: int,
    provider_name_snapshot: Optional[str],
    messages: list[dict[str, str]],
    temperature: float = 0,
    max_tokens: int = 8192,
) -> str:
    """Single ``chat_completion`` call + token usage accounting."""
    text = client.chat_completion(messages, temperature=temperature, max_tokens=max_tokens)
    _record_token(
        db,
        client=client,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
    )
    return text or ""


def generate_single_pass_summary_payload(
    *,
    db: Any,
    client: Any,
    provider_id: int,
    provider_name_snapshot: Optional[str],
    events: list[EventRecord],
    prompt: tuple[str, str],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
    max_tokens: int = 8192,
    locale: Optional[str] = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    """Run the single-pass LLM path; return the final structured payload.

    Returns ``(overall_summary, subject_sections, attention_items,
    prompt_chars, parse_retried)``.
    """
    system_prompt, user_prompt = prompt
    response_text = _chat(
        db=db,
        client=client,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    overall_summary = build_fallback_overall_summary(has_events=bool(events), locale=locale)
    structured_subject_sections: list[dict[str, Any]] = []
    structured_attention_items: list[dict[str, Any]] = []
    parse_retried = False

    if response_text:
        try:
            parsed = parse_daily_summary_output(response_text)
            overall_summary = parsed.overall_summary
            structured_subject_sections = [item.model_dump() for item in parsed.subject_sections]
            structured_attention_items = [item.model_dump() for item in parsed.attention_items]
        except DailySummaryOutputError as exc:
            parse_retried = True
            logger.warning("Single-pass summary parse failed, retry once: %s", exc)
            retry_text = _chat(
                db=db,
                client=client,
                provider_id=provider_id,
                provider_name_snapshot=provider_name_snapshot,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": get_retry_json_instruction(locale) + "\n\n" + user_prompt,
                    },
                ],
            )
            try:
                parsed = parse_daily_summary_output(retry_text)
                overall_summary = parsed.overall_summary
                structured_subject_sections = [
                    item.model_dump() for item in parsed.subject_sections
                ]
                structured_attention_items = [item.model_dump() for item in parsed.attention_items]
            except DailySummaryOutputError:
                overall_summary = build_fallback_overall_summary(
                    has_events=bool(events), locale=locale
                )
                structured_subject_sections = []
                structured_attention_items = []

    completed_sections = complete_subject_sections(
        sections=structured_subject_sections,
        known_subjects=known_subjects,
        subject_sections_payload=subject_sections_payload,
        locale=locale,
    )

    return (
        overall_summary,
        completed_sections,
        structured_attention_items,
        len(user_prompt),
        parse_retried,
    )


def generate_serial_summary_payload(
    *,
    db: Any,
    client: Any,
    provider_id: int,
    provider_name_snapshot: Optional[str],
    target_date: date,
    start_dt: datetime,
    end_dt: datetime,
    events: list[EventRecord],
    home_context: dict[str, Any],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
    missing_subjects: list[str],
    attention_candidates_payload: list[dict[str, Any]],
    max_tokens: int = 8192,
    locale: Optional[str] = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    """Run the per-subject + rollup serial LLM path.

    Returns ``(overall_summary, subject_sections, attention_items,
    prompt_chars_total, parse_retried)``. ``prompt_chars_total``
    accumulates the per-subject + rollup user-prompt lengths so the
    caller can write them to the ``task_log`` detail.
    """
    compressed_input = compress_daily_input(
        subject_sections=subject_sections_payload,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates_payload,
    )

    subject_type_by_name = {
        str(item.get("subject_name") or ""): str(item.get("subject_type") or "unknown")
        for item in known_subjects
        if str(item.get("subject_name") or "")
    }

    existing_names = {
        str(item.get("subject_name") or "") for item in compressed_input["subject_sections"]
    }
    for missing_name in missing_subjects:
        if missing_name in existing_names:
            continue
        compressed_input["subject_sections"].append(
            {
                "subject_name": missing_name,
                "subject_type": subject_type_by_name.get(missing_name, "unknown"),
                "raw_event_count": 0,
                "clusters": [],
            }
        )

    compressed_input["subject_sections"].sort(
        key=lambda item: int(item.get("raw_event_count") or 0),
        reverse=True,
    )

    overall_summary = build_fallback_overall_summary(has_events=bool(events), locale=locale)
    structured_subject_sections: list[dict[str, Any]] = []
    structured_attention_items: list[dict[str, Any]] = []
    prompt_chars_total = 0
    parse_retried = False

    for subject_section in compressed_input["subject_sections"]:
        subject_prompt = build_subject_summary_prompt(
            home_context=home_context,
            summary_date=target_date,
            time_range_start=start_dt.isoformat(),
            time_range_end=end_dt.isoformat(),
            subject_section=subject_section,
            locale=locale,
        )
        subject_system_prompt, subject_user_prompt = subject_prompt
        prompt_chars_total += len(subject_user_prompt)
        subject_name = str(subject_section.get("subject_name") or "\u672a\u77e5\u5bf9\u8c61")
        subject_type = str(subject_section.get("subject_type") or "unknown")

        subject_response_text = _chat(
            db=db,
            client=client,
            provider_id=provider_id,
            provider_name_snapshot=provider_name_snapshot,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": subject_system_prompt},
                {"role": "user", "content": subject_user_prompt},
            ],
        )

        try:
            subject_summary, attention_needed = parse_subject_summary_output(
                subject_response_text,
                subject_name=subject_name,
            )
        except DailySummaryOutputError as exc:
            parse_retried = True
            logger.warning("Subject summary parse failed, retry once: %s", exc)
            retry_text = _chat(
                db=db,
                client=client,
                provider_id=provider_id,
                provider_name_snapshot=provider_name_snapshot,
                max_tokens=max_tokens,
                messages=[
                    {
                        "role": "system",
                        "content": subject_system_prompt,
                    },
                    {
                        "role": "user",
                        "content": (
                            get_retry_json_instruction(locale) + "\n\n" + subject_user_prompt
                        ),
                    },
                ],
            )
            try:
                subject_summary, attention_needed = parse_subject_summary_output(
                    retry_text,
                    subject_name=subject_name,
                )
            except DailySummaryOutputError:
                subject_summary = build_subject_fallback_summary(subject_section, locale=locale)
                attention_needed = False

        structured_subject_sections.append(
            {
                "subject_name": subject_name,
                "subject_type": subject_type,
                "summary": clean_generated_text(subject_summary),
                "attention_needed": attention_needed,
                "activity_score": int(subject_section.get("raw_event_count") or 0),
            }
        )

    structured_subject_sections.sort(
        key=lambda item: int(item.get("activity_score") or 0),
        reverse=True,
    )

    rollup_prompt = build_daily_rollup_prompt(
        home_context=home_context,
        summary_date=target_date,
        subject_results=structured_subject_sections,
        attention_candidates=compressed_input["attention_candidates"],
        locale=locale,
    )
    rollup_system_prompt, rollup_user_prompt = rollup_prompt
    prompt_chars_total += len(rollup_user_prompt)

    rollup_response_text = _chat(
        db=db,
        client=client,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": rollup_system_prompt},
            {"role": "user", "content": rollup_user_prompt},
        ],
    )
    try:
        overall_summary, structured_attention_items = parse_rollup_output(rollup_response_text)
    except DailySummaryOutputError as exc:
        parse_retried = True
        logger.warning("Rollup summary parse failed, retry once: %s", exc)
        retry_text = _chat(
            db=db,
            client=client,
            provider_id=provider_id,
            provider_name_snapshot=provider_name_snapshot,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": rollup_system_prompt},
                {
                    "role": "user",
                    "content": get_retry_json_instruction(locale) + "\n\n" + rollup_user_prompt,
                },
            ],
        )
        try:
            overall_summary, structured_attention_items = parse_rollup_output(retry_text)
        except DailySummaryOutputError:
            overall_summary = build_fallback_overall_summary(has_events=bool(events), locale=locale)
            structured_attention_items = []

    return (
        overall_summary,
        structured_subject_sections,
        structured_attention_items,
        prompt_chars_total,
        parse_retried,
    )


__all__ = [
    "generate_serial_summary_payload",
    "generate_single_pass_summary_payload",
]
