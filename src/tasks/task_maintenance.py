"""Task maintenance heartbeat — thin wrapper over the orchestration module.

The heartbeat orchestrates the maintenance policies in
:mod:`src.services.maintenance` (hot scheduling, running-lease
recovery, orphan-pending recovery, hourly retention + missing-file
sweep). The task entry only delegates to :func:`run_heartbeat` in
:mod:`src.tasks._task_maintenance_orchestration`, which owns the whole
sweep including its DB session and the top-level commit / rollback /
failure envelope.
"""

from __future__ import annotations

from typing import Any

from src.core.celery_app import celery_app
from src.tasks._task_maintenance_orchestration import run_heartbeat


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def heartbeat(self: Any) -> dict[str, int]:
    """Run every maintenance policy in turn; report deterministic counters."""
    return run_heartbeat()


__all__ = ["heartbeat"]
