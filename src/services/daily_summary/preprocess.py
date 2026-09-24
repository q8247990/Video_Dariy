from collections import defaultdict
from typing import Literal, cast

from src.models.event_record import EventRecord
from src.services.attention import is_attention_event
from src.services.daily_summary.schemas import (
    AttentionCandidate,
    SubjectEventSection,
    SubjectEventSummary,
)

SubjectType = Literal["member", "pet"]


def build_known_subjects(home_context: dict) -> list[dict[str, str]]:
    subjects: list[dict[str, str]] = []

    for item in home_context.get("members", []):
        name = (item.get("name") or "").strip()
        if not name:
            continue
        subjects.append({"subject_name": name, "subject_type": "member"})

    for item in home_context.get("pets", []):
        name = (item.get("name") or "").strip()
        if not name:
            continue
        subjects.append({"subject_name": name, "subject_type": "pet"})

    return subjects


def build_subject_event_mapping(
    events: list[EventRecord],
    known_subjects: list[dict[str, str]],
) -> tuple[list[SubjectEventSection], list[str], set[int]]:
    known_name_to_type: dict[str, SubjectType] = {
        item["subject_name"]: cast(SubjectType, item["subject_type"]) for item in known_subjects
    }
    subject_event_map: dict[str, list[SubjectEventSummary]] = defaultdict(list)
    mapped_event_ids: set[int] = set()

    for event in events:
        related_entities = event.related_entities_json
        if not isinstance(related_entities, list):
            continue

        for entity in related_entities:
            if not isinstance(entity, dict):
                continue

            recognition_status = str(entity.get("recognition_status") or "unknown")
            if recognition_status not in {"confirmed", "suspected"}:
                continue

            matched_name = (
                entity.get("matched_profile_name") or entity.get("display_name") or ""
            ).strip()
            if not matched_name:
                continue
            if matched_name not in known_name_to_type:
                continue

            mapped_event_ids.add(event.id)
            subject_event_map[matched_name].append(
                SubjectEventSummary(
                    event_id=event.id,
                    event_type=event.event_type,
                    title=_event_title(event),
                    summary=_event_summary(event),
                    recognition_status=recognition_status,
                )
            )

    sections: list[SubjectEventSection] = []
    for subject_name, event_summaries in subject_event_map.items():
        if not event_summaries:
            continue
        sections.append(
            SubjectEventSection(
                subject_name=subject_name,
                subject_type=known_name_to_type[subject_name],
                related_event_count=len(event_summaries),
                related_event_summaries=event_summaries,
            )
        )

    sections.sort(key=lambda item: item.related_event_count, reverse=True)

    observed_subject_names = {item.subject_name for item in sections}
    missing_subjects = [
        item["subject_name"]
        for item in known_subjects
        if item["subject_name"] not in observed_subject_names
    ]
    return sections, missing_subjects, mapped_event_ids


def extract_attention_candidates(
    events: list[EventRecord],
    *,
    attention_keys: frozenset[str] = frozenset(),
) -> list[AttentionCandidate]:
    candidates: list[AttentionCandidate] = []

    for event in events:
        if not is_attention_event(
            event_type=event.event_type,
            related_entities=event.related_entities_json,
            focus_matches=event.focus_matches_json,
            attention_keys=attention_keys,
        ):
            continue

        candidates.append(
            AttentionCandidate(
                event_id=event.id,
                event_type=event.event_type,
                title=_event_title(event),
                summary=_event_summary(event),
            )
        )

    return candidates


def _event_title(event: EventRecord) -> str:
    if event.title and event.title.strip():
        return event.title.strip()
    if event.summary and event.summary.strip():
        return event.summary.strip()[:24]
    return event.description.strip()[:24] if event.description.strip() else f"事件 {event.id}"


def _event_summary(event: EventRecord) -> str:
    if event.summary and event.summary.strip():
        return event.summary.strip()
    if event.detail and event.detail.strip():
        return event.detail.strip()
    return event.description.strip() if event.description.strip() else _event_title(event)
