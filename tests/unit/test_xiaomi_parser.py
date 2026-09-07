import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.adapters.xiaomi_parser import XiaomiDirectoryParser


def test_parse_file_name_logs_invalid_timestamp(caplog) -> None:
    parser = XiaomiDirectoryParser("/videos")

    with caplog.at_level(logging.DEBUG, logger="src.adapters.xiaomi_parser"):
        result = parser.parse_file_name("2026130112", "01M00S_clip.mp4")

    assert result is None
    assert "folder=2026130112" in caplog.text
    assert "file=01M00S_clip.mp4" in caplog.text


def test_parse_file_name_localizes_camera_time_to_utc() -> None:
    parser = XiaomiDirectoryParser("/videos", timezone=ZoneInfo("Asia/Shanghai"))

    result = parser.parse_file_name("2026083016", "31M32S_1772091092.mp4")

    assert result is not None
    assert result["start_time"] == datetime(2026, 8, 30, 8, 31, 32, tzinfo=timezone.utc)
    assert result["end_time"] == datetime(2026, 8, 30, 8, 32, 32, tzinfo=timezone.utc)


def test_parse_file_name_without_timezone_stays_naive() -> None:
    parser = XiaomiDirectoryParser("/videos")

    result = parser.parse_file_name("2026083016", "31M32S_1772091092.mp4")

    assert result is not None
    assert result["start_time"] == datetime(2026, 8, 30, 16, 31, 32)
    assert result["start_time"].tzinfo is None


def test_get_directory_time_bounds_returns_utc_when_timezone_set(tmp_path) -> None:
    (tmp_path / "2026083016").mkdir()
    (tmp_path / "2026083018").mkdir()
    parser = XiaomiDirectoryParser(str(tmp_path), timezone=ZoneInfo("Asia/Shanghai"))

    earliest, latest = parser.get_directory_time_bounds()

    assert earliest == datetime(2026, 8, 30, 8, 0, 0, tzinfo=timezone.utc)
    assert latest == datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)


def test_scan_directory_filters_with_aware_boundaries(tmp_path) -> None:
    folder = tmp_path / "2026083016"
    folder.mkdir()
    (folder / "31M32S_1772091092.mp4").write_bytes(b"video")
    parser = XiaomiDirectoryParser(str(tmp_path), timezone=ZoneInfo("Asia/Shanghai"))

    # 16:00-17:00 local (08:00-09:00 UTC); file at 16:31 local is inside range
    results = parser.scan_directory(
        min_time=datetime(2026, 8, 30, 8, 0, 0, tzinfo=timezone.utc),
        max_time=datetime(2026, 8, 30, 9, 0, 0, tzinfo=timezone.utc),
    )

    assert len(results) == 1
    assert results[0]["start_time"] == datetime(2026, 8, 30, 8, 31, 32, tzinfo=timezone.utc)


def test_scan_directory_excludes_files_outside_aware_window(tmp_path) -> None:
    folder = tmp_path / "2026083016"
    folder.mkdir()
    (folder / "31M32S_1772091092.mp4").write_bytes(b"video")
    parser = XiaomiDirectoryParser(str(tmp_path), timezone=ZoneInfo("Asia/Shanghai"))

    # Window ends at 08:30 UTC (16:30 local), file starts 16:31 local -> excluded
    results = parser.scan_directory(
        min_time=datetime(2026, 8, 30, 7, 0, 0, tzinfo=timezone.utc),
        max_time=datetime(2026, 8, 30, 8, 30, 0, tzinfo=timezone.utc),
    )

    assert results == []
