"""Maintenance policy package.

The pre-Wave-5 ``src/tasks/task_maintenance.py`` heartbeat body
(~373 LOC) owned every maintenance concern: hot-build dispatch,
running-lease recovery, orphan-pending recovery, missing-file
reconciliation, and 7-day retention cleanup. Wave 5 splits it into
the discrete policy modules in this package + a thin aggregator in
``src/tasks/task_maintenance.py`` that calls each policy and reports
the deterministic counters.

Policy modules:

* :mod:`.constants` — shared tunables
  (``CLEANUP_DAYS``, ``HOT_BUILD_TIMEOUT_SECONDS``,
  ``FULL_BUILD_TIMEOUT_SECONDS``,
  ``ANALYSIS_BASE_TIMEOUT_SECONDS``,
  ``ANALYSIS_RETRY_GRACE_SECONDS``,
  ``DAILY_SUMMARY_TIMEOUT_SECONDS``).
* :mod:`.hot_scheduling` — :func:`dispatch_hot_builds`, the
  heartbeat-driven per-source HOT enqueue.
* :mod:`.running_lease_recovery` — :func:`recover_timed_out_tasks`,
  the "crashed worker" detector for ``status == RUNNING`` rows.
* :mod:`.analysis_recovery` — :func:`resume_lost_analysis`, the
  analysis-specific lease-expiry handler that delegates to the
  outbox dispatcher once the recovery budget allows it.
* :mod:`.unleased_recovery` — :func:`recover_unleased_pending_tasks`
  + :func:`recover_orphan_pending_tasks`, the broker-message-loss
  path.
* :mod:`.missing_file` — :func:`mark_missing_video_files`, the
  hourly reconciliation sweep.
* :mod:`.retention` — :func:`cleanup_old_task_logs`, the 7-day
  ``task_log`` retention cleanup.

Each policy module is independently unit-testable; the existing
``tests/unit/test_task_maintenance.py`` is the load-bearing
specification that every split must keep passing without
modification.
"""

from __future__ import annotations

from src.services.maintenance.analysis_recovery import resume_lost_analysis
from src.services.maintenance.constants import (
    ANALYSIS_BASE_TIMEOUT_SECONDS,
    CLEANUP_DAYS,
    DAILY_SUMMARY_TIMEOUT_SECONDS,
    FULL_BUILD_TIMEOUT_SECONDS,
    HOT_BUILD_TIMEOUT_SECONDS,
)
from src.services.maintenance.hot_scheduling import dispatch_hot_builds
from src.services.maintenance.missing_file import mark_missing_video_files
from src.services.maintenance.retention import cleanup_old_task_logs
from src.services.maintenance.running_lease_recovery import recover_timed_out_tasks
from src.services.maintenance.unleased_recovery import (
    recover_orphan_pending_tasks,
    recover_unleased_pending_tasks,
)

__all__ = [
    "ANALYSIS_BASE_TIMEOUT_SECONDS",
    "CLEANUP_DAYS",
    "DAILY_SUMMARY_TIMEOUT_SECONDS",
    "FULL_BUILD_TIMEOUT_SECONDS",
    "HOT_BUILD_TIMEOUT_SECONDS",
    "cleanup_old_task_logs",
    "dispatch_hot_builds",
    "mark_missing_video_files",
    "recover_orphan_pending_tasks",
    "recover_timed_out_tasks",
    "recover_unleased_pending_tasks",
    "resume_lost_analysis",
]
