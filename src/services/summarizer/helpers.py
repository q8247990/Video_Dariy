"""Pure-text helpers shared by the parse / generate / normalise stages.

None of these helpers touch the DB or the LLM gateway; they exist
to keep the parse / generate / normalise code focused on flow
control. The split / clamp logic lives in :func:`clamp_summary_payload`
(``normalization``); the ``complete_subject_sections`` helper that
adds the missing-subject fallbacks lives here because both the
single-pass and serial generation paths reach for it.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.core.i18n.locale_directive import (
    get_fallback_summary,
    get_subject_activity_text,
    get_subject_fallback_text,
    get_subject_no_activity_text,
)
from src.services.daily_summary.schemas import AttentionItem


def clean_generated_text(value: str) -> str:
    """Normalise whitespace / consecutive CJK punctuation in LLM text."""
    text = " ".join((value or "").strip().split())
    text = text.replace("\u2026", "\u3002")
    text = re.sub(r"\.{3,}", "\u3002", text)
    text = re.sub(r"\u3002{2,}", "\u3002", text)
    text = re.sub(r"\uff0c{2,}", "\uff0c", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    """Split ``text`` on sentence-ending punctuation; drop empties."""
    cleaned = clean_generated_text(text)
    if not cleaned:
        return []
    segments = re.split(r"(?<=[\u3002\uff01\uff1f!?])", cleaned)
    return [seg.strip() for seg in segments if seg.strip()]


def trim_sentences_to_chars(text: str, max_chars: int) -> str:
    """Return up to ``max_chars`` characters, preserving whole sentences."""
    sentences = split_sentences(text)
    if not sentences:
        return ""
    acc: list[str] = []
    total = 0
    for sent in sentences:
        if total + len(sent) > max_chars and acc:
            break
        if total + len(sent) > max_chars:
            return sent[:max_chars].strip(" \uff0c\u3002")
        acc.append(sent)
        total += len(sent)
    return "".join(acc).strip(" \uff0c\u3002")


def truncate_text(value: str, max_len: int) -> str:
    """Trim trailing whitespace; cap at ``max_len`` characters."""
    text = value.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip("\uff0c\u3002,. ")


def estimate_detail_length(
    subject_sections: list[dict[str, Any]], attention_items: list[dict[str, Any]]
) -> int:
    """Coarse estimate of how many chars a detail section will serialise to.

    Mirrors the legacy ``_estimate_detail_length`` from
    :mod:`src.tasks.summarizer` — counts the per-section name /
    summary plus a fixed 8-char overhead per item. The clamp
    machinery needs the number, not a precise rendering, so the
    heuristic is fine.
    """
    total = 0
    for item in subject_sections:
        if not isinstance(item, dict):
            continue
        total += len(str(item.get("subject_name") or ""))
        total += len(str(item.get("summary") or ""))
        total += 8
    for item in attention_items:
        if not isinstance(item, dict):
            continue
        total += len(str(item.get("title") or ""))
        total += len(str(item.get("summary") or ""))
        total += 8
    return total


def build_fallback_overall_summary(*, has_events: bool, locale: Optional[str] = None) -> str:
    """Return the stable empty-day / non-empty fallback text."""
    return get_fallback_summary(has_events, locale)


def build_subject_fallback_summary(
    subject_section: dict[str, Any], *, locale: Optional[str] = None
) -> str:
    """Per-subject fallback when the LLM rollup fails parsing."""
    subject_name = str(subject_section.get("subject_name") or "\u8be5\u5bf9\u8c61")
    raw_count = int(subject_section.get("raw_event_count") or 0)
    raw_clusters = subject_section.get("clusters")
    clusters: list[dict[str, Any]] = raw_clusters if isinstance(raw_clusters, list) else []
    return get_subject_fallback_text(subject_name, raw_count, len(clusters), locale)


def _build_subject_type_map(
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
) -> dict[str, str]:
    subject_type_map: dict[str, str] = {}
    for item in known_subjects:
        name = str(item.get("subject_name") or "").strip()
        if not name:
            continue
        subject_type_map[name] = str(item.get("subject_type") or "unknown")
    for item in subject_sections_payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("subject_name") or "").strip()
        if not name:
            continue
        subject_type_map[name] = str(
            item.get("subject_type") or subject_type_map.get(name) or "unknown"
        )
    return subject_type_map


def _build_activity_score_map(
    subject_sections_payload: list[dict[str, Any]],
) -> dict[str, int]:
    activity_score_map: dict[str, int] = {}
    for item in subject_sections_payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("subject_name") or "").strip()
        if not name:
            continue
        activity_score_map[name] = int(item.get("related_event_count") or 0)
    return activity_score_map


def _normalise_section(
    item: dict[str, Any],
    subject_type_map: dict[str, str],
    activity_score_map: dict[str, int],
) -> dict[str, Any] | None:
    name = str(item.get("subject_name") or "").strip()
    if not name:
        return None
    return {
        "subject_name": name,
        "subject_type": str(item.get("subject_type") or subject_type_map.get(name) or "unknown"),
        "summary": clean_generated_text(str(item.get("summary") or "")),
        "attention_needed": bool(item.get("attention_needed")),
        "activity_score": activity_score_map.get(name, 0),
    }


def _missing_subject_stub(
    name: str,
    subject_type: str,
    activity_score: int,
    locale: Optional[str],
) -> dict[str, Any]:
    if activity_score > 0:
        summary = get_subject_activity_text(name, activity_score, locale)
    else:
        summary = get_subject_no_activity_text(name, locale)
    return {
        "subject_name": name,
        "subject_type": subject_type,
        "summary": summary,
        "attention_needed": False,
        "activity_score": activity_score,
    }


def complete_subject_sections(
    *,
    sections: list[dict[str, Any]],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
    locale: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Merge LLM-produced sections with the known / missing-subject map.

    The LLM may legitimately omit a subject that had no events
    (``布丁`` was asleep all day); :func:`complete_subject_sections`
    walks the known subject map and inserts a "no activity" stub for
    each missing entry. The sort key is the activity score so the
    final roll-up lists active subjects first.
    """
    subject_type_map = _build_subject_type_map(known_subjects, subject_sections_payload)
    activity_score_map = _build_activity_score_map(subject_sections_payload)

    normalized: list[dict[str, Any]] = []
    existing_names: set[str] = set()
    for item in sections:
        if not isinstance(item, dict):
            continue
        record = _normalise_section(item, subject_type_map, activity_score_map)
        if record is None:
            continue
        existing_names.add(record["subject_name"])
        normalized.append(record)

    for subject_name, subject_type in subject_type_map.items():
        if subject_name in existing_names:
            continue
        score = activity_score_map.get(subject_name, 0)
        normalized.append(_missing_subject_stub(subject_name, subject_type, score, locale))

    normalized.sort(key=lambda item: int(item.get("activity_score") or 0), reverse=True)
    return normalized


def normalise_attention_items(
    attention_items: list[Any], *, max_count: int = 3
) -> list[dict[str, Any]]:
    """Best-effort coercion of LLM / serial-path attention items."""
    normalized: list[dict[str, Any]] = []
    for item in attention_items[:max_count]:
        if not isinstance(item, dict):
            continue
        try:
            normalized.append(AttentionItem.model_validate(item).model_dump())
        except Exception:
            title = str(item.get("title") or "\u672a\u547d\u540d\u5173\u6ce8\u9879").strip()
            summary = str(item.get("summary") or "").strip()
            if not title or not summary:
                continue
            level = str(item.get("level") or "medium").strip() or "medium"
            normalized.append({"title": title, "summary": summary, "level": level})
    return normalized


__all__ = [
    "build_fallback_overall_summary",
    "build_subject_fallback_summary",
    "clean_generated_text",
    "complete_subject_sections",
    "estimate_detail_length",
    "normalise_attention_items",
    "split_sentences",
    "trim_sentences_to_chars",
    "truncate_text",
]
