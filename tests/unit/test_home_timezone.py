from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src.services.home_timezone import format_local_day_range, local_day_bounds


def test_local_day_bounds_maps_shanghai_midnights_to_utc() -> None:
    start, end = local_day_bounds(ZoneInfo("Asia/Shanghai"), date(2026, 3, 13))

    assert start == datetime(2026, 3, 12, 16, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 13, 16, tzinfo=timezone.utc)


def test_local_day_bounds_handles_new_york_dst_spring_forward() -> None:
    start, end = local_day_bounds(ZoneInfo("America/New_York"), date(2026, 3, 8))

    assert start == datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 9, 4, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 23 * 60 * 60


def test_format_local_day_range_renders_utc_instants_as_shanghai_local_text() -> None:
    zone = ZoneInfo("Asia/Shanghai")
    start, end = local_day_bounds(zone, date(2026, 11, 22))

    text = format_local_day_range(start, end, zone)

    assert text == "2026-11-22 00:00 ~ 2026-11-23 00:00（Asia/Shanghai）"
    assert "+00:00" not in text
    assert "T16:00:00" not in text


def test_format_local_day_range_renders_fixed_offset_non_asia_zone() -> None:
    zone = ZoneInfo("Etc/GMT-8")  # fixed offset UTC+8
    start = datetime(2026, 3, 12, 16, tzinfo=timezone.utc)
    end = datetime(2026, 3, 13, 16, tzinfo=timezone.utc)

    assert format_local_day_range(start, end, zone) == (
        "2026-03-13 00:00 ~ 2026-03-14 00:00（Etc/GMT-8）"
    )
