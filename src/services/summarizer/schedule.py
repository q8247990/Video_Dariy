"""Scheduling helpers: home timezone, daily trigger and dispatch guard."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.core.pipeline_constants import TaskStatus
from src.models.daily_summary import DailySummary
from src.models.task_log import TaskLog
from src.services.app_runtime_state import (
    claim_runtime_state,
    clear_runtime_state,
)
from src.services.home_timezone import get_home_timezone
from src.services.onboarding import DEFAULT_DAILY_SUMMARY_SCHEDULE
from src.services.summarizer.contracts import DISPATCH_GUARD_STATE_KEY_PREFIX
from src.services.system_config_registry import (
    DAILY_SUMMARY_SCHEDULE,
    get_config,
)


# --- schedule.py ---
def get_daily_schedule(db: Session) -> str:
    """Read the configured ``daily_summary_schedule`` or the default."""
    value = get_config(db, DAILY_SUMMARY_SCHEDULE)
    return str(value) if value else DEFAULT_DAILY_SUMMARY_SCHEDULE


# --- schedule.py ---
def parse_schedule_time(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` into ``(hour, minute)``.

    Raises:
        ValueError: when the input is not a valid ``HH:MM`` value.
    """
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("schedule format must be HH:MM")

    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("schedule value out of range")
    return hour, minute


# --- schedule.py ---
def resolve_target_date(now: datetime) -> date:
    """Return the summary target date: yesterday in the supplied ``now``."""
    return (now - timedelta(days=1)).date()


# --- schedule.py ---
def _dispatch_guard_key(target_date: date) -> str:
    """Return the ``AppRuntimeState`` key for a per-date dispatch lock."""
    return f"{DISPATCH_GUARD_STATE_KEY_PREFIX}:{target_date.isoformat()}"


# --- schedule.py ---
def has_existing_summary_or_task(db: Session, target_date: date) -> bool:
    """Return ``True`` when a daily summary or in-flight task already exists.

    Both a published ``daily_summary`` row and a pending / running
    ``task_log`` count as "already exists"; the dispatcher skips
    both to keep the per-day generation idempotent.
    """
    exists_summary = (
        db.query(DailySummary).filter(DailySummary.summary_date == target_date).first() is not None
    )
    if exists_summary:
        return True

    date_str = str(target_date)
    task_log = (
        db.query(TaskLog)
        .filter(
            TaskLog.task_type == "daily_summary_generation",
            TaskLog.dedupe_key == f"daily_summary_generation|{date_str}",
            TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.SUCCESS]),
        )
        .order_by(TaskLog.created_at.desc())
        .first()
    )
    return task_log is not None


# --- schedule.py ---
def claim_dispatch_guard(db: Session, now: datetime, target_date: date) -> bool:
    """Atomically reserve the per-date dispatch slot.

    Backed by :func:`claim_runtime_state` which itself relies on the
    ``app_runtime_state`` table's UNIQUE key for the serialisation
    guarantee. ``False`` means another dispatch already holds the
    slot for this date.
    """
    return claim_runtime_state(
        db,
        _dispatch_guard_key(target_date),
        {
            "target_date": str(target_date),
            "scheduled_for_date": now.date().isoformat(),
            "dispatched_at": now.isoformat(),
            "task_id": None,
        },
    )


# --- schedule.py ---
def release_dispatch_guard(db: Session, target_date: date) -> None:
    """Clear the per-date dispatch slot so a retry can claim it again."""
    clear_runtime_state(db, _dispatch_guard_key(target_date))


# --- schedule.py ---
def scheduled_local_datetime(now: datetime, schedule_text: str, zone: ZoneInfo) -> datetime:
    """Return the local ``(today, HH:MM)`` datetime for ``schedule_text``."""
    hour, minute = parse_schedule_time(schedule_text)
    return datetime.combine(now.date(), time(hour, minute), tzinfo=zone)


__all__ = [
    "claim_dispatch_guard",
    "get_daily_schedule",
    "get_home_timezone",
    "has_existing_summary_or_task",
    "parse_schedule_time",
    "release_dispatch_guard",
    "resolve_target_date",
    "scheduled_local_datetime",
]
