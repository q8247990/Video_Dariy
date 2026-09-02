"""Observability metrics for the /metrics health surface (Todo 24).

Pulls the operational gauges a dashboard / operator wants to watch,
straight from the live database:

* **outbox lag** — age (seconds) of the oldest pending/publishing
  ``outbox_event`` row and the current pending / failed counts. The
  publisher already derives the same ``oldest_pending_seconds`` for its
  own health check; this module recomputes it at request time so the
  HTTP surface is independent of any one publisher instance.
* **task recovery** — the heartbeat persists its per-run counters
  (``timed_out`` = leases recovered, ``pending_recovered`` = unleased
  recovered) into ``AppRuntimeState`` under the
  ``heartbeat_last_counters`` key; this module surfaces the last
  snapshot plus a live cumulative gauge of analysis lease recoveries
  (``COUNT(task_log WHERE recovery_attempt > 0)``).
* **analysis checkpoint progress** — for each currently running
  ``session_analysis`` task, how many sub-chunks completed successfully
  out of the total allocated for the current ``analysis_run``.

All queries are read-only gauges computed against a caller-supplied
engine; nothing here mutates state. The endpoint is deliberately simple
JSON (no Prometheus dependency) and is unit- and PG-testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from src.models.app_runtime_state import AppRuntimeState
from src.models.outbox import OutboxEvent
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.services.pipeline_constants import TaskStatus, TaskType

#: AppRuntimeState key under which the heartbeat persists its counters.
HEARTBEAT_COUNTERS_KEY = "heartbeat_last_counters"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def outbox_metrics(engine: Engine) -> dict[str, Any]:
    """Return outbox lag / counts gauges."""
    with engine.connect() as conn:
        status_counts: dict[str, int] = {}
        for status_, count in conn.execute(
            select(OutboxEvent.status, func.count(OutboxEvent.id)).group_by(OutboxEvent.status)
        ):
            status_counts[str(status_)] = int(count)
        oldest_stmt = select(func.min(OutboxEvent.next_attempt_at)).where(
            OutboxEvent.status.in_(("pending", "publishing"))
        )
        oldest = conn.execute(oldest_stmt).scalar_one_or_none()
    lag_seconds: Optional[int] = None
    if oldest is not None:
        lag_seconds = int(max(0, (datetime.now(timezone.utc) - _as_utc(oldest)).total_seconds()))
    return {
        "oldest_pending_seconds": lag_seconds,
        "pending_count": int(status_counts.get("pending", 0)),
        "publishing_count": int(status_counts.get("publishing", 0)),
        "failed_count": int(status_counts.get("failed", 0)),
    }


def task_recovery_metrics(engine: Engine) -> dict[str, Any]:
    """Return task-recovery counters (heartbeat snapshot + live gauge)."""
    with engine.connect() as conn:
        leases_total = int(
            conn.execute(
                select(func.count(TaskLog.id)).where(TaskLog.recovery_attempt > 0)
            ).scalar_one()
            or 0
        )
        counters_row = conn.execute(
            select(AppRuntimeState.state_value).where(
                AppRuntimeState.state_key == HEARTBEAT_COUNTERS_KEY
            )
        ).scalar_one_or_none()
    last: dict[str, Any] = counters_row if isinstance(counters_row, dict) else {}
    return {
        # Cumulative gauge: every TaskLog that has been through analysis
        # lease recovery (the persistent recovery marker).
        "leases_recovered_total": leases_total,
        # Last heartbeat snapshot (counters since that specific sweep).
        "last_heartbeat": {
            "leases_recovered": int(last.get("timed_out", 0)),
            "unleased_recovered": int(last.get("pending_recovered", 0)),
            "dispatched_hot": int(last.get("dispatched_hot", 0)),
            "logs_deleted": int(last.get("logs_deleted", 0)),
            "missing_marked": int(last.get("missing_marked", 0)),
        },
    }


def checkpoint_progress(engine: Engine) -> list[dict[str, Any]]:
    """Return per-running-analysis checkpoint progress.

    Each running ``session_analysis`` task maps to a session; for its
    current ``analysis_run`` we report total checkpoints planned, how
    many reached ``success``, and how many are still pending/failed.
    """
    with engine.connect() as conn:
        running = conn.execute(
            select(TaskLog.task_target_id, TaskLog.id).where(
                TaskLog.task_type == TaskType.SESSION_ANALYSIS,
                TaskLog.status == TaskStatus.RUNNING,
                TaskLog.task_target_id.is_not(None),
            )
        )
        running_ids = []
        by_session: dict[int, int] = {}
        for session_id, task_log_id in running:
            if session_id is not None:
                running_ids.append(session_id)
                by_session.setdefault(session_id, task_log_id)
        progress_by_session: dict[int, dict[str, int]] = {}
        if running_ids:
            rows = conn.execute(
                select(
                    SessionAnalysisCheckpoint.session_id,
                    SessionAnalysisCheckpoint.state,
                    func.count(SessionAnalysisCheckpoint.id),
                )
                .where(SessionAnalysisCheckpoint.session_id.in_(running_ids))
                .group_by(SessionAnalysisCheckpoint.session_id, SessionAnalysisCheckpoint.state)
            )
            for session_id, state, count in rows:
                slot = progress_by_session.setdefault(session_id, {"success": 0, "total": 0})
                slot["total"] += int(count)
                slot["success"] += int(count) if state == "success" else 0
    results: list[dict[str, Any]] = []
    for session_id, task_log_id in by_session.items():
        slot = progress_by_session.get(session_id, {"success": 0, "total": 0})
        results.append(
            {
                "session_id": session_id,
                "task_log_id": task_log_id,
                "completed_sub_chunks": slot["success"],
                "total_sub_chunks": slot["total"],
                "running": slot["success"] if slot["total"] else 0,
            }
        )
    return results


def metrics_snapshot(engine: Engine) -> dict[str, Any]:
    """Compose the full /metrics payload."""
    return {
        "outbox": outbox_metrics(engine),
        "task_recovery": task_recovery_metrics(engine),
        "analysis_checkpoint": checkpoint_progress(engine),
    }


__all__ = [
    "HEARTBEAT_COUNTERS_KEY",
    "checkpoint_progress",
    "metrics_snapshot",
    "outbox_metrics",
    "task_recovery_metrics",
]
