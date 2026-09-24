"""Rule-derived attention classification.

Replaces the model's subjective ``importance_level`` with one deterministic
rule shared by session aggregation, the daily summary and the query layer.
"""

from __future__ import annotations

from typing import Any, Iterable

from src.schemas.home_profile import FocusPointItem

ATTENTION_EVENT_TYPES = frozenset(
    {"unknown_person_appear", "abnormal_stay", "scene_attention_needed"}
)


def has_unknown_person(related_entities: Any) -> bool:
    if not isinstance(related_entities, list):
        return False
    return any(
        isinstance(entity, dict) and str(entity.get("entity_type") or "") == "unknown_person"
        for entity in related_entities
    )


def attention_focus_keys(items: Iterable[FocusPointItem]) -> frozenset[str]:
    return frozenset(item.key for item in items if item.enabled and item.attention)


def attention_keys_for_db(db: Any) -> frozenset[str]:
    from src.models.home_profile import HomeProfile
    from src.schemas.home_profile import coerce_focus_points

    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    if profile is None:
        return frozenset()
    return attention_focus_keys(coerce_focus_points(profile.focus_points_json))


def is_attention_event(
    *,
    event_type: str | None,
    related_entities: Any = None,
    focus_matches: Any = None,
    attention_keys: frozenset[str] = frozenset(),
) -> bool:
    if (event_type or "") in ATTENTION_EVENT_TYPES:
        return True
    if has_unknown_person(related_entities):
        return True
    if attention_keys and isinstance(focus_matches, list):
        return any(str(key) in attention_keys for key in focus_matches)
    return False


__all__ = [
    "ATTENTION_EVENT_TYPES",
    "attention_focus_keys",
    "attention_keys_for_db",
    "has_unknown_person",
    "is_attention_event",
]
