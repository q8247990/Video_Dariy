"""Performance / call-count regression tests for ``XiaomiDirectoryParser.scan_directory``.

These tests pin the parser's internal optimisation behaviour —
folder pre-filtering without an extra ``stat`` call and the
throttled cancel-check cadence — rather than its observable output.
They are white-box: they assert on ``os.path.isdir`` invocation
counts and on the iteration index at which ``cancel_check`` is
invoked, both of which are implementation details.

白盒测试：直接调用内部 stage 函数/类，断言绑定实现细节，随实现重构，不作为接口契约回归基线。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.adapters.xiaomi_parser import XiaomiDirectoryParser


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
