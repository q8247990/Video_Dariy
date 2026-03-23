from datetime import datetime
from pathlib import Path

from src.adapters.xiaomi_parser import XiaomiDirectoryParser


def test_parse_file_name():
    parser = XiaomiDirectoryParser("/mock/path")

    # Correct format
    result = parser.parse_file_name("2026022615", "31M32S_1772091092.mp4")
    assert result is not None
    assert result["start_time"] == datetime(2026, 2, 26, 15, 31, 32)
    assert result["duration_seconds"] == 60

    # Invalid folder format (not a date string)
    result2 = parser.parse_file_name("invalid_folder", "31M32S_1772091092.mp4")
    assert result2 is None

    # Invalid file format
    result3 = parser.parse_file_name("2026022615", "random_video.mp4")
    assert result3 is None


def test_scan_directory_with_max_time_and_bounds(tmp_path: Path) -> None:
    folder_old = tmp_path / "2026031410"
    folder_new = tmp_path / "2026031510"
    folder_old.mkdir(parents=True, exist_ok=True)
    folder_new.mkdir(parents=True, exist_ok=True)

    (folder_old / "00M00S_1.mp4").write_bytes(b"1")
    (folder_new / "00M00S_2.mp4").write_bytes(b"2")

    parser = XiaomiDirectoryParser(str(tmp_path))
    earliest, latest = parser.get_directory_time_bounds()
    assert earliest == datetime(2026, 3, 14, 10, 0, 0)
    assert latest == datetime(2026, 3, 15, 10, 0, 0)

    rows = parser.scan_directory(max_time=datetime(2026, 3, 14, 23, 59, 59))
    assert len(rows) == 1
    assert rows[0]["file_name"] == "00M00S_1.mp4"
