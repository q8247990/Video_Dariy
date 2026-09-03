"""Session build Celery tasks: ``hot_build_task`` and ``full_build_task``.

Two thin Celery tasks sharing the same orchestrator in
:mod:`src.tasks._session_build_orchestration`. The slim tasks
open the ``task_db_session`` and delegate the whole pipeline
(TaskLog binding, scan-window policy, the five stages, TaskLog
finalize, analyzer dispatch) to the orchestration module; the
stages (discovery / dedupe / reducer / seal policy) all live in
:mod:`src.services.session_build` so they can be unit-tested
without a Celery broker.

The re-exported names (``HOT_WINDOW_HOURS`` / ``_compute_full_scan_end``
/ ``_dispatch_analysis_for_sealed``) preserve the monkey-patchable seam
the existing test suite relies on: they resolve at
``src.tasks.session_build.<name>`` unchanged.
"""

from __future__ import annotations

from typing import Any

from src.core.celery_app import celery_app
from src.db.session import task_db_session
from src.tasks._session_build_orchestration import (  # noqa: F401 - re-exported seam
    HOT_WINDOW_HOURS,
    _compute_full_scan_end,
    _dispatch_analysis_for_sealed,
    run_full_build,
    run_hot_build,
)


@celery_app.task(bind=True, time_limit=3600)  # type: ignore[untyped-decorator]
def hot_build_task(self: Any, source_id: int) -> dict[str, Any]:
    """HOT-mode task: heartbeat-driven scan of recent files."""
    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    with task_db_session() as db:
        return run_hot_build(db, source_id=source_id, queue_task_id=queue_task_id)


@celery_app.task(bind=True, time_limit=259200)  # type: ignore[untyped-decorator]  # 3 days
def full_build_task(self: Any, source_id: int) -> dict[str, Any]:
    """FULL-mode task: user-driven scan of the entire history up to the hot boundary."""
    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    with task_db_session() as db:
        return run_full_build(db, source_id=source_id, queue_task_id=queue_task_id)


__all__ = [
    "HOT_WINDOW_HOURS",
    "_compute_full_scan_end",
    "_dispatch_analysis_for_sealed",
    "full_build_task",
    "hot_build_task",
]
