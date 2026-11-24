"""LLM generation: parsers, clamp/normalisation and the single/serial payload builders."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from src.core.i18n.locale_directive import get_retry_json_instruction
from src.models.event_record import EventRecord
from src.services.daily_summary.output_parser import (
    DailySummaryOutputError,
    parse_daily_summary_output,
)
from src.services.llm_output_utils import extract_json_object, strip_code_block
from src.services.llm_qos import record_token_usage
from src.services.prompt_builder.compression.daily_summary_compressor import (
    compress_daily_input,
)
from src.services.prompt_builder.v2.daily_summary import (
    build_daily_rollup_prompt,
    build_subject_summary_prompt,
)
from src.services.summarizer.prepare import (
    build_fallback_overall_summary,
    build_subject_fallback_summary,
    clean_generated_text,
    complete_subject_sections,
    estimate_detail_length,
    normalise_attention_items,
    trim_sentences_to_chars,
    truncate_for_summary,
)


# --- parsing.py ---
def extract_json_payload(raw_text: str) -> dict[str, Any]:
    """Strip code fences, find the JSON object, return it as ``dict``.

    Raises:
        :class:`DailySummaryOutputError` when no JSON object is found
        or the body cannot be parsed.
    """
    text = strip_code_block(raw_text or "")
    try:
        payload = extract_json_object(text)
    except ValueError as exc:
        raise DailySummaryOutputError(f"Invalid JSON output: {exc}") from exc
    if not isinstance(payload, dict):
        raise DailySummaryOutputError("LLM JSON response must be an object")
    return payload


# --- parsing.py ---
def parse_subject_summary_output(raw_text: str, *, subject_name: str) -> tuple[str, bool]:
    """Parse the per-subject serial-path output.

    Returns ``(summary, attention_needed)``. Falls back to the
    truncated ``overall_summary`` when ``summary`` is empty but the
    structured ``subject_sections`` list contains a match. The
    fallback exists because some models emit the same envelope for
    both subject and rollup calls — when the per-subject call
    returns a full summary structure we still want a usable answer.

    Raises:
        :class:`DailySummaryOutputError` when no usable summary can
        be extracted.
    """
    payload = extract_json_payload(raw_text)

    summary = str(payload.get("summary") or "").strip()
    attention_needed = bool(payload.get("attention_needed"))
    if summary:
        return summary, attention_needed

    if isinstance(payload.get("subject_sections"), list):
        parsed = parse_daily_summary_output(raw_text)
        matched = None
        for item in parsed.subject_sections:
            if item.subject_name == subject_name:
                matched = item
                break
        target = matched or (parsed.subject_sections[0] if parsed.subject_sections else None)
        if target is not None:
            return target.summary, bool(target.attention_needed)

    fallback_text = str(payload.get("overall_summary") or "").strip()
    if fallback_text:
        return truncate_for_summary(fallback_text, 120), False

    raise DailySummaryOutputError("subject summary output missing summary")


# --- parsing.py ---
def parse_rollup_output(raw_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse the daily-rollup call's output.

    Returns ``(overall_summary, attention_items)``. When the rollup
    fails validation but emits a structured ``subject_sections``
    list, we still recover a usable ``overall_summary`` from
    :func:`parse_daily_summary_output` so the caller can decide
    between the retry path and the fallback.
    """
    payload = extract_json_payload(raw_text)

    overall_summary = str(payload.get("overall_summary") or "").strip()
    attention_items = payload.get("attention_items")

    if not overall_summary and isinstance(payload.get("subject_sections"), list):
        parsed = parse_daily_summary_output(raw_text)
        return parsed.overall_summary, [item.model_dump() for item in parsed.attention_items]

    normalised = normalise_attention_items(
        attention_items if isinstance(attention_items, list) else []
    )

    if not overall_summary:
        raise DailySummaryOutputError("rollup output missing overall_summary")

    return overall_summary, normalised


# --- normalization.py ---
_MAX_TOTAL_CHARS: int = 900


# --- normalization.py ---
_DEFAULT_OVERALL_MIN: int = 120


# --- normalization.py ---
_SUBJECT_BUDGET_CHARS: int = 80


# --- normalization.py ---
_ATTENTION_BUDGET_CHARS: int = 60


# --- normalization.py ---
def clamp_summary_payload(
    overall_summary: str,
    subject_sections: list[dict[str, Any]],
    attention_items: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(overall, sections, attention)`` clamped to the budget."""
    normalized_sections: list[dict[str, Any]] = []
    for item in subject_sections:
        if not isinstance(item, dict):
            continue
        current = dict(item)
        current["summary"] = clean_generated_text(str(current.get("summary") or ""))
        normalized_sections.append(current)

    normalized_attention: list[dict[str, Any]] = []
    for item in attention_items[:3]:
        if not isinstance(item, dict):
            continue
        current = dict(item)
        current["summary"] = clean_generated_text(str(current.get("summary") or ""))
        normalized_attention.append(current)

    normalized_overall = clean_generated_text(overall_summary)

    detail_len = estimate_detail_length(normalized_sections, normalized_attention)
    if len(normalized_overall) + detail_len <= _MAX_TOTAL_CHARS:
        return normalized_overall, normalized_sections, normalized_attention

    # 1) clamp overall first while leaving a full-sentence budget
    max_overall_len = max(_DEFAULT_OVERALL_MIN, _MAX_TOTAL_CHARS - detail_len)
    clamped_overall = trim_sentences_to_chars(normalized_overall, max_overall_len)

    if len(clamped_overall) + detail_len <= _MAX_TOTAL_CHARS:
        return clamped_overall, normalized_sections, normalized_attention

    # 2) clamp each subject summary to 80 chars
    for item in normalized_sections:
        item["summary"] = trim_sentences_to_chars(
            str(item.get("summary") or ""), _SUBJECT_BUDGET_CHARS
        )

    detail_len = estimate_detail_length(normalized_sections, normalized_attention)
    if len(clamped_overall) + detail_len <= _MAX_TOTAL_CHARS:
        return clamped_overall, normalized_sections, normalized_attention

    # 3) finally clamp each attention summary to 60 chars
    for item in normalized_attention:
        item["summary"] = trim_sentences_to_chars(
            str(item.get("summary") or ""), _ATTENTION_BUDGET_CHARS
        )

    return clamped_overall, normalized_sections, normalized_attention


# --- generation.py ---
logger = logging.getLogger(__name__)


# --- generation.py ---
def _record_token(
    db: Any,
    *,
    client: Any,
    provider_id: int,
    provider_name_snapshot: str | None,
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


# --- generation.py ---
def _chat(
    db: Any,
    *,
    client: Any,
    provider_id: int,
    provider_name_snapshot: str | None,
    messages: list[dict[str, str]],
    temperature: float = 0,
    max_tokens: int = 8192,
    extra_body: dict[str, Any] | None = None,
) -> str:
    """Single ``chat_completion`` call + token usage accounting."""
    text = client.chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )
    _record_token(
        db,
        client=client,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
    )
    return text or ""


# --- generation.py ---
def generate_single_pass_summary_payload(
    *,
    db: Any,
    client: Any,
    provider_id: int,
    provider_name_snapshot: str | None,
    events: list[EventRecord],
    prompt: tuple[str, str],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
    max_tokens: int = 8192,
    request_extra_body: dict[str, Any] | None = None,
    locale: str | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    """Run the single-pass LLM path; return the final structured payload.

    Returns ``(overall_summary, subject_sections, attention_items,
    prompt_chars, parse_retried)``.

    An empty day never reaches the model: the honest data-missing fallback
    is returned with zero LLM calls (and therefore no ``LLMUsageLog`` row),
    so "the system collected nothing" is not reframed as "the home was calm".
    """
    if not events:
        return (
            build_fallback_overall_summary(has_events=False, locale=locale),
            [],
            [],
            0,
            False,
        )

    system_prompt, user_prompt = prompt
    response_text = _chat(
        db=db,
        client=client,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
        max_tokens=max_tokens,
        extra_body=request_extra_body,
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
                extra_body=request_extra_body,
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


# --- generation.py ---
def generate_serial_summary_payload(
    *,
    db: Any,
    client: Any,
    provider_id: int,
    provider_name_snapshot: str | None,
    target_date: date,
    time_range_start: str,
    time_range_end: str,
    events: list[EventRecord],
    home_context: dict[str, Any],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
    missing_subjects: list[str],
    attention_candidates_payload: list[dict[str, Any]],
    max_tokens: int = 8192,
    request_extra_body: dict[str, Any] | None = None,
    locale: str | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    """Run the per-subject + rollup serial LLM path.

    ``time_range_start`` / ``time_range_end`` carry the pre-formatted
    LOCAL day-range text (``format_local_day_range`` output) rendered into
    the per-subject prompts; raw UTC ISO instants must not reach the model.

    Returns ``(overall_summary, subject_sections, attention_items,
    prompt_chars_total, parse_retried)``. ``prompt_chars_total``
    accumulates the per-subject + rollup user-prompt lengths so the
    caller can write them to the ``task_log`` detail.

    An empty day never reaches the model: the honest data-missing fallback
    is returned with zero LLM calls (and therefore no ``LLMUsageLog`` row).
    """
    if not events:
        return (
            build_fallback_overall_summary(has_events=False, locale=locale),
            [],
            [],
            0,
            False,
        )

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
            time_range_start=time_range_start,
            time_range_end=time_range_end,
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
            extra_body=request_extra_body,
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
                extra_body=request_extra_body,
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
        extra_body=request_extra_body,
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
            extra_body=request_extra_body,
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
    "clamp_summary_payload",
    "extract_json_payload",
    "generate_serial_summary_payload",
    "generate_single_pass_summary_payload",
    "parse_rollup_output",
    "parse_subject_summary_output",
]
