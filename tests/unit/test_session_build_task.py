from datetime import datetime, timedelta

from src.tasks.session_build import HOT_WINDOW_HOURS, _compute_full_scan_end


def test_compute_full_scan_end_uses_fixed_window_boundary() -> None:
    now = datetime(2026, 3, 15, 12, 0, 0)

    assert _compute_full_scan_end(now) == now - timedelta(hours=HOT_WINDOW_HOURS)
