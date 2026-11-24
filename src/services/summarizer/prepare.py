"""Input preparation: text helpers, evidence building and the Evidence bundle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.core.i18n.locale_directive import (
    get_fallback_summary,
    get_subject_activity_text,
    get_subject_fallback_text,
    get_subject_no_activity_text,
)
from src.models.event_record import EventRecord
from src.schemas.home_profile import coerce_focus_points
from src.services.attention import attention_focus_keys
from src.services.daily_summary.preprocess import (
    build_known_subjects,
    build_subject_event_mapping,
    extract_attention_candidates,
)
from src.services.daily_summary.schemas import AttentionItem
from src.services.home_profile import build_home_context
from src.services.home_timezone import format_local_day_range, local_day_bounds


# --- helpers.py ---
def clean_generated_text(value: str) -> str:
    """Normalise whitespace / consecutive CJK punctuation in LLM text."""
    text = " ".join((value or "").strip().split())
    text = text.replace("\u2026", "\u3002")
    text = re.sub(r"\.{3,}", "\u3002", text)
    text = re.sub(r"\u3002{2,}", "\u3002", text)
    text = re.sub(r"\uff0c{2,}", "\uff0c", text)
    return text.strip()


# --- helpers.py ---
def split_sentences(text: str) -> list[str]:
    """Split ``text`` on sentence-ending punctuation; drop empties."""
    cleaned = clean_generated_text(text)
    if not cleaned:
        return []
    segments = re.split(r"(?<=[\u3002\uff01\uff1f!?])", cleaned)
    return [seg.strip() for seg in segments if seg.strip()]


# --- helpers.py ---
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


# --- helpers.py ---
def truncate_for_summary(value: str, max_len: int) -> str:
    """Trim trailing whitespace; cap at ``max_len`` characters."""
    text = value.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip("\uff0c\u3002,. ")


# --- helpers.py ---
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


# --- helpers.py ---
def build_fallback_overall_summary(*, has_events: bool, locale: str | None = None) -> str:
    """Return the stable empty-day / non-empty fallback text."""
    return get_fallback_summary(has_events, locale)


# --- helpers.py ---
def build_subject_fallback_summary(
    subject_section: dict[str, Any], *, locale: str | None = None
) -> str:
    """Per-subject fallback when the LLM rollup fails parsing."""
    subject_name = str(subject_section.get("subject_name") or "\u8be5\u5bf9\u8c61")
    raw_count = int(subject_section.get("raw_event_count") or 0)
    raw_clusters = subject_section.get("clusters")
    clusters: list[dict[str, Any]] = raw_clusters if isinstance(raw_clusters, list) else []
    return get_subject_fallback_text(subject_name, raw_count, len(clusters), locale)


# --- helpers.py ---
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


# --- helpers.py ---
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


# --- helpers.py ---
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


# --- helpers.py ---
def _missing_subject_stub(
    name: str,
    subject_type: str,
    activity_score: int,
    locale: str | None,
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


# --- helpers.py ---
def complete_subject_sections(
    *,
    sections: list[dict[str, Any]],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
    locale: str | None = None,
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


# --- helpers.py ---
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


# --- evidence.py ---
PromptBuilder = Callable[..., tuple[str, str]]


# --- evidence.py ---
@dataclass(frozen=True)
class Evidence:
    """All inputs the generation stage needs to produce a summary.

    Attributes:
        target_date: The summary date (timezone-naive ``date``).
        start_dt / end_dt: UTC half-open range of the day's events.
        home_context: Snapshot of ``build_home_context(db)`` —
            home profile, members and pets.
        known_subjects: Compact ``[{"subject_name", "subject_type"}]``
            list extracted from the home context.
        subject_sections: Per-subject roll-up of events.
        missing_subjects: Known subjects with zero events.
        attention_candidates: Operator-facing attention candidates.
        events: Ordered list of the day's ``EventRecord`` rows.
        prompt: The ``(system, user)`` prompt tuple.
        prompt_chars: ``len(prompt[1])`` — convenient for the
            single-vs-serial decision.
        locale: Locale directive passed to the prompt builder.
        single_pass_user_prompt_chars: ``len(prompt[1])`` —
            duplicated for the orchestrator's split-decision logic.
        time_range_start: Local-text day range (``format_local_day_range``
            output, e.g. ``"2026-11-22 00:00 ~ 2026-11-23 00:00（Asia/Shanghai）"``)
            fed to the prompt builders in place of the raw UTC ISO strings.
            The whole range rides in this field so the templates keep the
            ``time_range_start`` / ``time_range_end`` variable names; the end
            text is embedded in the start text.
        time_range_end: Kept for the two-string prompt contract; empty in
            the current single-string rendering.
    """

    target_date: date
    start_dt: datetime
    end_dt: datetime
    home_context: dict[str, Any]
    known_subjects: list[dict[str, str]]
    subject_sections: list[dict[str, Any]]
    missing_subjects: list[str]
    attention_candidates: list[dict[str, Any]]
    events: list[EventRecord]
    prompt: tuple[str, str]
    prompt_chars: int
    locale: str | None = None
    single_pass_user_prompt_chars: int = 0
    time_range_start: str = ""
    time_range_end: str = ""


# --- evidence.py ---
def build_evidence(
    db: Session,
    *,
    target_date: date,
    zone: ZoneInfo,
    prompt_builder: PromptBuilder,
    prompt_input_factory: Callable[..., Any],
    locale: str | None = None,
) -> Evidence:
    """Build an :class:`Evidence` for ``target_date`` using ``zone``.

    Args:
        db: Caller-owned ``Session``; the helper runs one events
            query and reads the home profile. The caller owns the
            commit / rollback.
        target_date: The date the summary covers.
        zone: Home timezone (``ZoneInfo`` instance).
        prompt_builder: Callable that builds the daily-summary
            prompt. Signature ``(prompt_input) -> (system, user)``.
            The production wiring supplies
            :func:`src.application.prompt.compiler.compile_daily_summary_prompt`.
        prompt_input_factory: Callable that builds the
            ``DailySummaryPromptInput`` payload from the evidence
            fields. The production wiring supplies
            :class:`src.application.prompt.contracts.DailySummaryPromptInput`.
            Accepting the factory as a parameter keeps
            :mod:`src.services.summarizer` free of
            :mod:`src.application.*` imports.
        locale: Optional locale directive.
    """
    start_dt, end_dt = local_day_bounds(zone, target_date)
    events = (
        db.query(EventRecord)
        .filter(
            EventRecord.event_start_time >= start_dt,
            EventRecord.event_start_time < end_dt,
        )
        .order_by(EventRecord.event_start_time.asc())
        .all()
    )

    home_context = build_home_context(db)
    known_subjects = build_known_subjects(home_context)
    subject_sections, missing_subjects, _ = build_subject_event_mapping(events, known_subjects)
    attention_keys = attention_focus_keys(
        coerce_focus_points(home_context["home_profile"].get("focus_items"))
    )
    attention_candidates = extract_attention_candidates(events, attention_keys=attention_keys)

    subject_sections_payload = [item.model_dump() for item in subject_sections]
    attention_candidates_payload = [item.model_dump() for item in attention_candidates]

    # Prompts must carry the LOCAL wall-clock range with an explicit timezone
    # label; raw UTC ISO strings get read as local time by the model.
    time_range_start = format_local_day_range(start_dt, end_dt, zone)
    time_range_end = ""

    prompt_input = prompt_input_factory(
        home_context=home_context,
        summary_date=target_date,
        time_range_start=time_range_start,
        time_range_end=time_range_end,
        subject_sections=subject_sections_payload,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates_payload,
        locale=locale,
    )
    prompt = prompt_builder(prompt_input)

    return Evidence(
        target_date=target_date,
        start_dt=start_dt,
        end_dt=end_dt,
        home_context=home_context,
        known_subjects=known_subjects,
        subject_sections=subject_sections_payload,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates_payload,
        events=events,
        prompt=prompt,
        prompt_chars=len(prompt[1]),
        locale=locale,
        single_pass_user_prompt_chars=len(prompt[1]),
        time_range_start=time_range_start,
        time_range_end=time_range_end,
    )


__all__ = [
    "Evidence",
    "PromptBuilder",
    "build_evidence",
    "build_fallback_overall_summary",
    "build_subject_fallback_summary",
    "clean_generated_text",
    "complete_subject_sections",
    "estimate_detail_length",
    "normalise_attention_items",
    "split_sentences",
    "trim_sentences_to_chars",
    "truncate_for_summary",
]
