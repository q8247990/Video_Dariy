"""Task-maintenance heartbeat orchestration — the heartbeat body.

The Celery task in :mod:`src.tasks.task_maintenance` is a thin
delegate. This module owns the whole heartbeat lifetime: it opens a
:func:`task_db_session`, runs the sweep, and keeps the top-level
commit / rollback / failure envelope. Each policy (hot scheduling /
running-lease recovery / orphan-pending recovery / hourly retention +
missing-file sweep) lives in :mod:`src.services.maintenance`; this
module only aggregates their deterministic counters into the heartbeat
report and persists them for the /metrics surface.

Helper names (``datetime``, ``dispatch_hot_builds``,
``mark_missing_video_files``, ...) are module-level imports called
directly; tests that need to stub one monkeypatch the name in this
module's namespace
(``src.tasks._task_maintenance_orchestration.X``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.db.session import task_db_session
from src.services.maintenance import (
    cleanup_old_task_logs,
    dispatch_hot_builds,
    mark_missing_video_files,
    recover_orphan_pending_tasks,
    recover_timed_out_tasks,
)

logger = logging.getLogger(__name__)


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


def run_heartbeat() -> dict[str, int]:
    """Run every maintenance policy in turn; report deterministic counters.

    Zero complex branching: each policy call returns a deterministic
    counter, the hourly-only cleanup / missing-file sweeps are gated on
    ``now.minute == 0``, and the final result dict is the contract the
    supervisor / dashboard consumes. On any policy failure the sweep
    rolls back and re-raises; the per-policy try / except lives inside
    the policy module itself (a single bad source cannot poison the
    hot-build sweep, for example).
    """
    with task_db_session() as db:
        try:
            now = datetime.now(timezone.utc)
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
    "_persist_heartbeat_counters",
    "run_heartbeat",
]
