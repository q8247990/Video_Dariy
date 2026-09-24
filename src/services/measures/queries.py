"""Concrete measure queries sharing the home-local-day window."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, or_, text
from sqlalchemy.orm import Session

from src.models.event_record import EventRecord
from src.models.home_profile import HomeProfile
from src.models.video_source import VideoSource
from src.schemas.home_profile import FocusPointItem, coerce_focus_points
from src.services.attention import ATTENTION_EVENT_TYPES, attention_focus_keys
from src.services.home_timezone import home_now, local_day_bounds
from src.services.summarizer.schedule import get_home_timezone


def home_local_day_window(db: Session) -> tuple[datetime, datetime]:
    zone = get_home_timezone(db)
    return local_day_bounds(zone, home_now(zone).date())


def declared_focus_items(db: Session) -> list[FocusPointItem]:
    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    if profile is None:
        return []
    return coerce_focus_points(profile.focus_points_json)


def attention_condition(db: Session) -> ColumnElement[bool]:
    conditions: list[Any] = [EventRecord.event_type.in_(sorted(ATTENTION_EVENT_TYPES))]
    conditions.append(
        text(
            "EXISTS (SELECT 1 FROM json_array_elements(event_record.related_entities_json) e "
            "WHERE e->>'entity_type' = 'unknown_person')"
        )
    )
    keys = sorted(attention_focus_keys(declared_focus_items(db)))
    if keys:
        keys_literal = ", ".join(f"'{key}'" for key in keys)
        conditions.append(
            text(f"(event_record.focus_matches_json::jsonb ?| array[{keys_literal}])")
        )
    return or_(*conditions)


def attention_event_count(db: Session, *, start: datetime, end: datetime) -> int:
    return int(
        db.query(func.count(EventRecord.id))
        .filter(
            EventRecord.event_start_time >= start,
            EventRecord.event_start_time < end,
            attention_condition(db),
        )
        .scalar()
        or 0
    )


def focus_event_counts(db: Session, *, start: datetime, end: datetime) -> list[tuple[str, int]]:
    rows = db.execute(
        text(
            "SELECT elem AS focus_key, count(*) AS cnt "
            "FROM event_record, "
            "json_array_elements_text(event_record.focus_matches_json) AS elem "
            "WHERE event_record.event_start_time >= :start "
            "AND event_record.event_start_time < :end "
            "GROUP BY elem ORDER BY cnt DESC"
        ),
        {"start": start, "end": end},
    ).all()
    return [(str(row.focus_key), int(row.cnt)) for row in rows]


def attention_event_rows(db: Session, *, limit: int = 5) -> list[tuple[EventRecord, VideoSource]]:
    rows = (
        db.query(EventRecord, VideoSource)
        .join(VideoSource, EventRecord.source_id == VideoSource.id)
        .filter(attention_condition(db))
        .order_by(EventRecord.event_start_time.desc())
        .limit(limit)
        .all()
    )
    return [(row[0], row[1]) for row in rows]


__all__ = [
    "attention_condition",
    "attention_event_count",
    "attention_event_rows",
    "declared_focus_items",
    "focus_event_counts",
    "home_local_day_window",
]
