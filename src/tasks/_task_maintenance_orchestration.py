"""Task-maintenance heartbeat orchestration — the heartbeat body.

The Celery task in :mod:`src.tasks.task_maintenance` is a thin wrapper:
it opens a :func:`task_db_session` and delegates the whole sweep to
:func:`run_heartbeat` in this module, keeping only the top-level
commit / rollback / failure envelope in the task entry. Each policy
(hot scheduling / running-lease recovery / orphan-pending recovery /
hourly retention + missing-file sweep) lives in
:mod:`src.services.maintenance`; this module only aggregates their
deterministic counters into the heartbeat report and persists them for
the /metrics surface.

Like the other ``_*_orchestration`` modules, every seam that the test
suite monkeypatches on ``src.tasks.task_maintenance`` (``datetime``,
``mark_missing_video_files``, ...) is read dynamically off that module
at call time, so a ``monkeypatch.setattr("src.tasks.task_maintenance.X",
...)`` in a test takes effect unchanged.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


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


def run_heartbeat(db: Session) -> dict[str, int]:
    """Run every maintenance policy in turn; report deterministic counters.

    Zero complex branching: each policy call returns a deterministic
    counter, the hourly-only cleanup / missing-file sweeps are gated on
    ``now.minute == 0``, and the final result dict is the contract the
    supervisor / dashboard consumes. On any policy failure the caller
    (the task entry) rolls back and re-raises; the per-policy try /
    except lives inside the policy module itself (a single bad source
    cannot poison the hot-build sweep, for example).

    Late import preserves the legacy ``src.tasks.task_maintenance.<name>``
    monkeypatch surface used by the integration suite.
    """
    from src.tasks import task_maintenance as tm

    now = tm.datetime.now(tm.timezone.utc)
    dispatched_hot = tm.dispatch_hot_builds(db)
    leases_recovered = tm.recover_timed_out_tasks(db, now)
    pending_recovered = tm.recover_orphan_pending_tasks(db, now)

    logs_deleted = 0
    missing_marked = 0
    if now.minute == 0:
        logs_deleted = tm.cleanup_old_task_logs(db, now)
        missing_marked = tm.mark_missing_video_files(db)

    counters = {
        "dispatched_hot": len(dispatched_hot),
        "timed_out": leases_recovered,
        "pending_recovered": pending_recovered,
        "logs_deleted": logs_deleted,
        "missing_marked": missing_marked,
    }
    _persist_heartbeat_counters(db, counters)
    return counters


__all__ = [
    "_persist_heartbeat_counters",
    "run_heartbeat",
]
