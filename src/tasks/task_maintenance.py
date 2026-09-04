"""Task maintenance heartbeat — thin wrapper over the orchestration module.

The heartbeat orchestrates the maintenance policies in
:mod:`src.services.maintenance` (hot scheduling, running-lease
recovery, orphan-pending recovery, hourly retention + missing-file
sweep). This task entry only opens a ``task_db_session`` and delegates
the sweep to :func:`run_heartbeat` in
:mod:`src.tasks._task_maintenance_orchestration`, keeping the top-level
commit / rollback / failure envelope in the task body.

Backwards-compat surface
========================

The re-exported names below preserve the monkey-patchable seam the
existing test suite relies on: ``datetime`` / ``timezone`` /
``mark_missing_video_files`` / ``get_container`` / ``celery_app`` /
``task_db_session`` and the legacy private policy aliases
(``_dispatch_hot_builds`` / ``_recover_timed_out_tasks`` /
``_recover_orphan_pending_tasks`` / ``_mark_missing_video_files``)
keep resolving at
``src.tasks.task_maintenance.<name>`` unchanged. The orchestration
reads every seam dynamically off this module at call time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone  # noqa: F401  (re-exported for legacy test seam)
from typing import Any

from src.core.celery_app import (
    celery_app,  # noqa: F401  (re-exported for legacy monkeypatch surface)
)
from src.db.session import task_db_session
from src.services.maintenance import (
    cleanup_old_task_logs,
    dispatch_hot_builds,
    mark_missing_video_files,
    recover_orphan_pending_tasks,
    recover_timed_out_tasks,
)
from src.tasks._container import (
    get_container,  # noqa: F401  (re-exported for legacy monkeypatch surface)
)
from src.tasks._task_maintenance_orchestration import run_heartbeat

logger = logging.getLogger(__name__)

# Re-export the policy helpers under their pre-Wave-5 private names so
# the existing tests keep passing without modification.
_dispatch_hot_builds = dispatch_hot_builds
_recover_timed_out_tasks = recover_timed_out_tasks
_recover_orphan_pending_tasks = recover_orphan_pending_tasks
_mark_missing_video_files = mark_missing_video_files


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def heartbeat(self: Any) -> dict[str, int]:
    """Run every maintenance policy in turn; report deterministic counters.

    Thin wrapper: opens a ``task_db_session``, delegates the sweep to
    :func:`run_heartbeat`, and owns the top-level commit / rollback /
    failure envelope. Zero complex branching lives in the orchestration.
    """
    with task_db_session() as db:
        try:
            counters = run_heartbeat(db)
            db.commit()
            return counters
        except Exception:
            db.rollback()
            logger.exception("Heartbeat failed")
            raise


__all__ = [
    "_dispatch_hot_builds",
    "_mark_missing_video_files",
    "_recover_orphan_pending_tasks",
    "_recover_timed_out_tasks",
    "celery_app",
    "cleanup_old_task_logs",
    "get_container",
    "heartbeat",
]
