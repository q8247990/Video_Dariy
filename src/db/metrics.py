"""Observability metrics for the /metrics health surface.

Pulls the operational gauges a dashboard / operator wants to watch,
straight from the live database:

* **delivery lag** — age (seconds) of the oldest ``task_delivery``
  intent row plus the pending / failed counts. Intents are deleted once
  the broker accepts them, so a non-zero count means a commit-then-send
  gap or a broker failure the heartbeat retry sweep has not cleared yet.
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
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from src.core.pipeline_constants import TaskStatus, TaskType
from src.models.app_runtime_state import AppRuntimeState
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_delivery import TaskDelivery
from src.models.task_log import TaskLog

#: AppRuntimeState key under which the heartbeat persists its counters.
HEARTBEAT_COUNTERS_KEY = "heartbeat_last_counters"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def delivery_metrics(engine: Engine) -> dict[str, Any]:
    """Return dispatch-delivery gauges (undelivered intents and failures)."""
    with engine.connect() as conn:
        pending_count = int(conn.execute(select(func.count(TaskDelivery.id))).scalar_one() or 0)
        failed_count = int(
            conn.execute(
                select(func.count(TaskDelivery.id)).where(TaskDelivery.last_error.is_not(None))
            ).scalar_one()
            or 0
        )
        oldest = conn.execute(select(func.min(TaskDelivery.created_at))).scalar_one_or_none()
    lag_seconds: int | None = None
    if oldest is not None:
        lag_seconds = int(max(0, (datetime.now(timezone.utc) - _as_utc(oldest)).total_seconds()))
    return {
        "oldest_pending_seconds": lag_seconds,
        "pending_count": pending_count,
        "failed_count": failed_count,
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
            }
        )
    return results


def metrics_snapshot(engine: Engine) -> dict[str, Any]:
    """Compose the full /metrics payload."""
    return {
        "delivery": delivery_metrics(engine),
        "task_recovery": task_recovery_metrics(engine),
        "analysis_checkpoint": checkpoint_progress(engine),
    }


__all__ = [
    "HEARTBEAT_COUNTERS_KEY",
    "checkpoint_progress",
    "metrics_snapshot",
    "delivery_metrics",
    "task_recovery_metrics",
]
