"""Parsing helpers for the daily-summary LLM output shapes.

The summarizer drives three LLM-call shapes — one for the single
pass, one for the per-subject roll (serial path) and one for the
final daily rollup. Each shape maps to its own parse helper:

* :func:`parse_summary_with_retry` — single-pass path. Re-parses the
  initial response; on failure, retries the same prompt with the
  structured-output retry instruction.
* :func:`parse_subject_summary_output` — per-subject call in the
  serial path.
* :func:`parse_rollup_output` — final daily rollup call in the
  serial path.

The retry helpers never call the LLM themselves — they return the
parsed payload and let :mod:`src.services.summarizer.generation`
drive the LLM call. This keeps the parser a pure function over the
LLM's text output, which the existing unit tests rely on.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.i18n.locale_directive import (
    get_retry_json_instruction,
    get_retry_structured_instruction,
    get_retry_system_role,
)
from src.services.daily_summary.output_parser import (
    DailySummaryOutputError,
    parse_daily_summary_output,
)
from src.services.llm_output_utils import extract_json_object, strip_code_block
from src.services.summarizer.helpers import normalise_attention_items, truncate_text

logger = logging.getLogger(__name__)


def parse_summary_with_retry(
    *,
    retry_client: Any,
    initial_response_text: Optional[str],
    prompt: str,
    locale: Optional[str] = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Parse ``initial_response_text``; on failure, retry once via ``retry_client``.

    Args:
        retry_client: Callable with the LLMGatewayPort protocol
            (``chat_completion(messages, temperature, max_tokens)``).
            The Celery task supplies the live gateway; tests supply a
            stub.
        initial_response_text: The LLM's first response, or ``None``
            when the model returned empty.
        prompt: The original single-pass user prompt, re-attached to
            the retry instruction.
        locale: Optional locale directive for the retry messages.

    Returns:
        Tuple ``(overall_summary, subject_sections, attention_items,
        parse_retried)``. ``parse_retried`` is ``True`` only when the
        initial parse failed and the retry path executed.
    """
    if initial_response_text:
        try:
            parsed = parse_daily_summary_output(initial_response_text)
            return (
                parsed.overall_summary,
                [item.model_dump() for item in parsed.subject_sections],
                [item.model_dump() for item in parsed.attention_items],
                False,
            )
        except DailySummaryOutputError as exc:
            logger.warning("Initial summary parse failed, retry with compact prompt: %s", exc)

    retry_prompt = "\n\n".join(
        [
            get_retry_structured_instruction(locale),
            prompt,
        ]
    )
    retry_text = retry_client.chat_completion(
        [
            {"role": "system", "content": get_retry_system_role(locale)},
            {"role": "user", "content": retry_prompt},
        ],
        temperature=0,
        max_tokens=8192,
    )
    parsed = parse_daily_summary_output(retry_text or "")
    return (
        parsed.overall_summary,
        [item.model_dump() for item in parsed.subject_sections],
        [item.model_dump() for item in parsed.attention_items],
        True,
    )


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
        return truncate_text(fallback_text, 120), False

    raise DailySummaryOutputError("subject summary output missing summary")


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


def retry_parse_after_failure(
    *,
    retry_client: Any,
    messages: list[dict[str, str]],
    parse_fn: Any,
    locale: Optional[str] = None,
) -> tuple[Any, ...]:
    """Send one retry message; parse the reply via ``parse_fn``.

    Used by the generation paths when the initial LLM call returned
    a payload that failed parsing. ``parse_fn`` is typically
    :func:`parse_subject_summary_output` or
    :func:`parse_rollup_output`.
    """
    retry_messages = [
        {"role": "system", "content": messages[0]["content"]},
        {
            "role": "user",
            "content": get_retry_json_instruction(locale) + "\n\n" + messages[1]["content"],
        },
    ]
    retry_text = retry_client.chat_completion(retry_messages, temperature=0, max_tokens=8192)
    parsed = parse_fn(retry_text)
    return (parsed,)


__all__ = [
    "extract_json_payload",
    "parse_rollup_output",
    "parse_subject_summary_output",
    "parse_summary_with_retry",
    "retry_parse_after_failure",
]
