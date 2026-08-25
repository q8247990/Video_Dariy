"""Home-timezone clock and local-day boundary helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


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
