"""Output-clamp helpers — keep ``overall + detail`` inside 500-900 chars.

The clamp is a *content* policy, not a structural contract: the
``DailySummary`` rows accept arbitrarily large
``overall_summary`` / ``subject_sections_json`` /
``attention_items_json`` payloads, but the frontend summary card
renders a fixed-height block and bailing out at 900 chars keeps the
visual rhythm consistent.

The algorithm mirrors the legacy
``_clamp_summary_payload`` from :mod:`src.tasks.summarizer`:

1. clean + cap attention items at 3;
2. clamp overall summary to a sentence-preserving budget;
3. clamp each subject summary to a tight 80-char budget;
4. clamp each attention summary to 60 chars if the total still
   exceeds 900.

Each clamp step preserves whole sentences (via
:func:`trim_sentences_to_chars`) so we never split a half-sentence
in the middle.
"""

from __future__ import annotations

from typing import Any

from src.services.summarizer.helpers import (
    clean_generated_text,
    estimate_detail_length,
    trim_sentences_to_chars,
)

_MAX_TOTAL_CHARS: int = 900
_DEFAULT_OVERALL_MIN: int = 120
_SUBJECT_BUDGET_CHARS: int = 80
_ATTENTION_BUDGET_CHARS: int = 60


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


__all__ = ["clamp_summary_payload"]
