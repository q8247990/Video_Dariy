"""Home-timezone clock and local-day boundary helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.services.system_config_registry import HOME_TIMEZONE, get_config


def get_home_timezone(db: Session) -> ZoneInfo:
    """Return the configured home timezone (the single runtime source)."""
    return ZoneInfo(str(get_config(db, HOME_TIMEZONE)))


def home_now(zone: ZoneInfo) -> datetime:
    """Return the current instant expressed in the configured home timezone."""
    return datetime.now(zone)


def local_day_bounds(zone: ZoneInfo, target_date: date) -> tuple[datetime, datetime]:
    """Return UTC instants bracketing one local calendar day as a half-open range."""
    local_start = datetime.combine(target_date, time.min, tzinfo=zone)
    next_local_start = datetime.combine(
        date.fromordinal(target_date.toordinal() + 1), time.min, tzinfo=zone
    )
    return (
        local_start.astimezone(timezone.utc),
        next_local_start.astimezone(timezone.utc),
    )


def format_local_day_range(start_utc: datetime, end_utc: datetime, zone: ZoneInfo) -> str:
    """Render UTC instants as human-readable LOCAL day-range text.

    ``local_day_bounds`` returns UTC instants; feeding their ``isoformat()``
    straight into a prompt makes the model read the UTC wall clock as local
    time (e.g. "2026-11-21T16:00:00+00:00" rendered as "下午4点" instead of
    the actual local midnight). This helper converts both instants back to
    ``zone`` and renders ``"YYYY-MM-DD HH:MM ~ YYYY-MM-DD HH:MM（<zone.key>）"``
    so prompts always carry the local clock plus an explicit timezone label.

    Args:
        start_utc: Timezone-aware instant for the range start (as returned
            by :func:`local_day_bounds`).
        end_utc: Timezone-aware instant for the range end (exclusive).
        zone: Home timezone used for the conversion and the label.
    """
    start_local = start_utc.astimezone(zone)
    end_local = end_utc.astimezone(zone)
    return (
        f"{start_local.strftime('%Y-%m-%d %H:%M')} ~ "
        f"{end_local.strftime('%Y-%m-%d %H:%M')}（{zone.key}）"
    )
