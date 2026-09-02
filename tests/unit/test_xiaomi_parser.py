import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

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


def _track_isdir(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real_isdir = os.path.isdir

    def counting_isdir(path):
        calls.append(path)
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", counting_isdir)
    return calls


def test_scan_directory_skips_out_of_window_folders_without_stat(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for folder_name in ("2026083012", "2026083014", "2026083016", "2026083020", "2026083021"):
        (tmp_path / folder_name).mkdir()
    (tmp_path / "2026083016" / "31M32S_1772091092.mp4").write_bytes(b"video")
    isdir_calls = _track_isdir(monkeypatch)
    parser = XiaomiDirectoryParser(str(tmp_path), timezone=ZoneInfo("Asia/Shanghai"))

    # Window 06:00-12:00 UTC = 14:00-20:00 local.
    results = parser.scan_directory(
        min_time=datetime(2026, 8, 30, 6, 0, 0, tzinfo=timezone.utc),
        max_time=datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert len(results) == 1
    # Only the three in-window folders may be stat'ed, never the others.
    assert sorted(isdir_calls) == sorted(
        [str(tmp_path / "2026083014"), str(tmp_path / "2026083016"), str(tmp_path / "2026083020")]
    )


def test_scan_directory_still_stats_nonconforming_folders(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "2026083016").mkdir()
    (tmp_path / "misc").mkdir()
    isdir_calls = _track_isdir(monkeypatch)
    parser = XiaomiDirectoryParser(str(tmp_path))

    results = parser.scan_directory()

    assert results == []
    assert sorted(isdir_calls) == sorted([str(tmp_path / "2026083016"), str(tmp_path / "misc")])


def test_scan_directory_throttles_cancel_check(tmp_path) -> None:
    for day in range(1, 11):
        for hour in range(24):
            (tmp_path / f"202601{day:02d}{hour:02d}").mkdir()
    for hour in range(10):
        (tmp_path / f"20260111{hour:02d}").mkdir()

    calls = []
    parser = XiaomiDirectoryParser(str(tmp_path))
    results = parser.scan_directory(cancel_check=lambda: calls.append(1))

    assert results == []
    # 250 directory entries: checked at iterations 100 and 200 only.
    assert calls == [1, 1]


def test_scan_directory_cancel_check_still_raises(tmp_path) -> None:
    for day in range(1, 6):
        for hour in range(24):
            (tmp_path / f"202601{day:02d}{hour:02d}").mkdir()

    def cancel_check() -> None:
        raise RuntimeError("cancelled")

    parser = XiaomiDirectoryParser(str(tmp_path))
    with pytest.raises(RuntimeError, match="cancelled"):
        parser.scan_directory(cancel_check=cancel_check)
