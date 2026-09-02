"""Dispatch-control policy package.

The pre-Wave-5 ``src/services/task_dispatch_control.py`` (415 LOC)
owned every dispatch-control concern: dedupe key construction,
active-row lookup, pending claim, worker-bind idempotency, lease
renewal, terminal finalization, user-cancellation observation, and
the HOT↔FULL precedence rule. Wave 5 splits that monolith into the
discrete policy modules in this package:

* :mod:`.constants` — the shared ``TERMINAL_TASK_STATUSES`` tuple.
* :mod:`.dedupe` — :func:`build_dedupe_key`,
  :func:`ensure_dict_detail`, :func:`find_duplicate_active_task`,
  :func:`is_singleton_task_running`. The pure dedupe / lookup helpers.
* :mod:`.claim` — :func:`create_pending_task_log`. The "no active
  row yet, insert a PENDING row" branch.
* :mod:`.worker_bind` — :func:`bind_or_create_running_task_log`. The
  consumer-side idempotency seam where a Celery ``task_id`` meets
  the TaskLog lifecycle.
* :mod:`.lease` — :func:`renew_task_lease`. The lease / heartbeat
  renewal.
* :mod:`.finalize` — :func:`finalize_task_log`,
  :func:`finalize_cancelled_task_log`. Terminal status writes.
* :mod:`.cancel` — :class:`TaskCancellationRequested`,
  :func:`ensure_task_not_cancelled`,
  :func:`is_task_cancel_requested`,
  :func:`get_task_log_for_update`. User-cancellation observation.
* :mod:`.deferred` — :func:`record_deferred_hot_scan`,
  :func:`supersede_active_hot_scan`. The HOT↔FULL precedence rule.

Each policy module is independently testable against a minimal
SQLite TaskLog table; the existing
``tests/unit/test_task_dispatch_binding.py`` is the load-bearing
specification that every split must keep passing without
modification.
"""

from __future__ import annotations

from src.services.dispatch.cancel import (
    TaskCancellationRequested,
    ensure_task_not_cancelled,
    get_task_log_for_update,
    is_task_cancel_requested,
)
from src.services.dispatch.claim import create_pending_task_log
from src.services.dispatch.dedupe import (
    build_dedupe_key,
    ensure_dict_detail,
    find_duplicate_active_task,
    is_singleton_task_running,
)
from src.services.dispatch.deferred import (
    record_deferred_hot_scan,
    supersede_active_hot_scan,
)
from src.services.dispatch.finalize import (
    finalize_cancelled_task_log,
    finalize_task_log,
)
from src.services.dispatch.lease import renew_task_lease
from src.services.dispatch.worker_bind import bind_or_create_running_task_log

__all__ = [
    "TaskCancellationRequested",
    "bind_or_create_running_task_log",
    "build_dedupe_key",
    "create_pending_task_log",
    "ensure_dict_detail",
    "ensure_task_not_cancelled",
    "finalize_cancelled_task_log",
    "finalize_task_log",
    "find_duplicate_active_task",
    "get_task_log_for_update",
    "is_singleton_task_running",
    "is_task_cancel_requested",
    "record_deferred_hot_scan",
    "renew_task_lease",
    "supersede_active_hot_scan",
]
