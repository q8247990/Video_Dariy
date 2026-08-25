from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src.services.home_timezone import local_day_bounds


def test_local_day_bounds_maps_shanghai_midnights_to_utc() -> None:
    start, end = local_day_bounds(ZoneInfo("Asia/Shanghai"), date(2026, 3, 13))

    assert start == datetime(2026, 3, 12, 16, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 13, 16, tzinfo=timezone.utc)


def test_local_day_bounds_handles_new_york_dst_spring_forward() -> None:
    start, end = local_day_bounds(ZoneInfo("America/New_York"), date(2026, 3, 8))

    assert start == datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 9, 4, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 23 * 60 * 60
