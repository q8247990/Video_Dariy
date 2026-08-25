import logging

from src.adapters.xiaomi_parser import XiaomiDirectoryParser


def test_parse_file_name_logs_invalid_timestamp(caplog) -> None:
    parser = XiaomiDirectoryParser("/videos")

    with caplog.at_level(logging.DEBUG, logger="src.adapters.xiaomi_parser"):
        result = parser.parse_file_name("2026130112", "01M00S_clip.mp4")

    assert result is None
    assert "folder=2026130112" in caplog.text
    assert "file=01M00S_clip.mp4" in caplog.text
