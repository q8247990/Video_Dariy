"""Task maintenance heartbeat — thin aggregator over the maintenance policies.

Wave 5 reduced the pre-Wave-5 monolith in this module (~373 LOC,
mixed hot-build dispatch + lease recovery + missing-file sweep +
retention cleanup) into a thin Celery task whose body is one call
per policy module:

1. :func:`src.services.maintenance.hot_scheduling.dispatch_hot_builds`
2. :func:`src.services.maintenance.running_lease_recovery.recover_timed_out_tasks`
3. :func:`src.services.maintenance.unleased_recovery.recover_orphan_pending_tasks`
4. :func:`src.services.maintenance.retention.cleanup_old_task_logs`
   (only at ``minute == 0``)
5. :func:`src.services.maintenance.missing_file.mark_missing_video_files`
   (only at ``minute == 0``)

Each policy module owns its own branching; the aggregator just
collects the deterministic counters and produces the heartbeat
result dict. The legacy private helpers
(``_dispatch_hot_builds`` / ``_recover_timed_out_tasks`` /
``_recover_orphan_pending_tasks`` / ``_mark_missing_video_files``)
are kept as **re-exports** so the existing
``tests/unit/test_task_maintenance.py`` /
``tests/unit/test_tasks_di.py`` keep passing unchanged — the
private ``_`` prefix is preserved by binding the names directly on
this module.

The cleanup / missing-file cadence is preserved verbatim from the
pre-Wave-5 monolith (``now.minute == 0`` — one sweep per hour).

Backwards-compat surface
========================

This module also re-exports ``celery_app`` and ``get_container`` so
the existing tests'
``monkeypatch.setattr("src.tasks.task_maintenance.celery_app.control.revoke", ...)``
and ``monkeypatch.setattr("src.tasks.task_maintenance.get_container", ...)``
keep working without modification. The Celery / container objects
are the same module-level singletons the policy modules use, so the
monkeypatch lands on the shared object reference (Celery's
``control.revoke`` is an attribute on the shared ``celery_app``
instance, not on this module).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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


def _persist_heartbeat_counters(db: Any, counters: dict[str, int]) -> None:
    """Upsert the heartbeat counters into AppRuntimeState for the /metrics surface.

    The maintenance policies report deterministic per-sweep counters
    (``timed_out`` = leases recovered, ``pending_recovered`` = unleased
    recovered). Persisting them lets the /metrics endpoint surface the
    recovery counters without any cross-process state. Runs inside the
    heartbeat transaction; guarded to be a no-op when the table is not
    present (hermetic SQLite unit-test contexts).
    """
    from sqlalchemy import inspect

    from src.db.metrics import HEARTBEAT_COUNTERS_KEY
    from src.models.app_runtime_state import AppRuntimeState

    if db.bind is None or not inspect(db.bind).has_table(AppRuntimeState.__tablename__):
        return
    row = db.query(AppRuntimeState).filter_by(state_key=HEARTBEAT_COUNTERS_KEY).first()
    if row is None:
        db.add(AppRuntimeState(state_key=HEARTBEAT_COUNTERS_KEY, state_value=dict(counters)))
    else:
        row.state_value = dict(counters)


logger = logging.getLogger(__name__)

# Re-export the policy helpers under their pre-Wave-5 private names so
# the existing ``tests/unit/test_task_maintenance.py`` /
# ``tests/unit/test_tasks_di.py`` keep passing without modification.
_dispatch_hot_builds = dispatch_hot_builds
_recover_timed_out_tasks = recover_timed_out_tasks
_recover_orphan_pending_tasks = recover_orphan_pending_tasks
_mark_missing_video_files = mark_missing_video_files
_cleanup_old_task_logs = cleanup_old_task_logs


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def heartbeat(self: Any) -> dict[str, Any]:
    """Run every maintenance policy in turn; report deterministic counters.

    The aggregator body has zero complex branching: each policy call
    returns a deterministic counter, the hourly-only cleanup /
    missing-file sweeps are gated on ``now.minute == 0``, and the
    final result dict is the contract the supervisor / dashboard
    consumes.

    On any policy failure the heartbeat rolls back the session and
    re-raises so Celery records the failure; the per-policy try /
    except lives inside the policy module itself (a single bad
    source cannot poison the hot-build sweep, for example).
    """
    with task_db_session() as db:
        now = datetime.now(timezone.utc)
        try:
            dispatched_hot = dispatch_hot_builds(db)
            leases_recovered = recover_timed_out_tasks(db, now)
            pending_recovered = recover_orphan_pending_tasks(db, now)

            logs_deleted = 0
            missing_marked = 0
            if now.minute == 0:
                logs_deleted = cleanup_old_task_logs(db, now)
                missing_marked = mark_missing_video_files(db)

            counters = {
                "dispatched_hot": len(dispatched_hot),
                "timed_out": leases_recovered,
                "pending_recovered": pending_recovered,
                "logs_deleted": logs_deleted,
                "missing_marked": missing_marked,
            }
            _persist_heartbeat_counters(db, counters)
            db.commit()
            return counters
        except Exception:
            db.rollback()
            logger.exception("Heartbeat failed")
            raise


__all__ = [
    "_cleanup_old_task_logs",
    "_dispatch_hot_builds",
    "_mark_missing_video_files",
    "_recover_orphan_pending_tasks",
    "_recover_timed_out_tasks",
    "celery_app",
    "get_container",
    "heartbeat",
]
