"""Shared constants for the maintenance policy modules.

The maintenance package splits the pre-Wave-5 monolithic
``src/tasks/task_maintenance.py`` heartbeat body (~373 LOC) into
discrete policy modules + a thin aggregator. The constants here are
the per-policy tunables (timeout windows, retention thresholds,
recovery budgets) that several policies share.

Behavioural notes:

* ``HOT_BUILD_TIMEOUT_SECONDS = 3600`` and
  ``FULL_BUILD_TIMEOUT_SECONDS = 259200`` (3 days) — the per-task-type
  timeout windows for the running-lease recovery policy.
* ``ANALYSIS_BASE_TIMEOUT_SECONDS = 600`` — the floor for the
  analysis lease-expiry timeout (the actual value scales with the
  recovery budget: ``ANALYSIS_PROGRESS_GRACE_SECONDS + retry_count * 60``).
* ``ANALYSIS_RECOVERY_MAX_ATTEMPTS`` — sourced from
  :data:`src.core.config.settings` so operators can tune it via env.
* ``CLEANUP_DAYS = 7`` — the TaskLog retention window. The
  ``pipeline_transition_log`` and ``daily_summary_attempt`` audit
  rows outlive this window because their FKs are
  ``ON DELETE SET NULL`` rather than ``CASCADE`` / ``RESTRICT``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

CLEANUP_DAYS: int = 7
HOT_BUILD_TIMEOUT_SECONDS: int = 3600
FULL_BUILD_TIMEOUT_SECONDS: int = 259200  # 3 days
ANALYSIS_BASE_TIMEOUT_SECONDS: int = 600
DAILY_SUMMARY_TIMEOUT_SECONDS: int = 300
ANALYSIS_RETRY_GRACE_SECONDS: int = 60


def cleanup_threshold(now: datetime) -> datetime:
    """Return the wall-clock instant older than which terminal TaskLogs are deleted.

    Helper exposed as a function (rather than a constant) so tests can
    pin ``now`` deterministically.
    """
    return now - timedelta(days=CLEANUP_DAYS)


__all__ = [
    "ANALYSIS_BASE_TIMEOUT_SECONDS",
    "ANALYSIS_RETRY_GRACE_SECONDS",
    "CLEANUP_DAYS",
    "DAILY_SUMMARY_TIMEOUT_SECONDS",
    "FULL_BUILD_TIMEOUT_SECONDS",
    "HOT_BUILD_TIMEOUT_SECONDS",
    "cleanup_threshold",
]
