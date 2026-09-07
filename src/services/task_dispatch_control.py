"""Task dispatch control: thin façade for backward compatibility.

Wave 5 split the pre-Wave-5 monolith in this module (415 LOC,
mixed dedupe / claim / worker-bind / lease / finalize / cancel /
deferred concerns) into the discrete policy modules under
:mod:`src.services.dispatch`. This file is now a **thin façade**
that re-exports every public name so the existing
``from src.services.task_dispatch_control import X`` call sites
keep working unchanged.

History
=======

* The pre-Wave-5 monolith owned the entire TaskLog lifecycle
  policy surface in one file: dedupe key construction, active-row
  lookup, pending claim, worker-bind idempotency, lease renewal,
  terminal finalization, user-cancellation observation, and the
  HOT↔FULL precedence rule.

* Wave 5 splits it into seven policy modules (one per concern)
  plus a ``constants`` module, each independently unit-testable
  against a minimal TaskLog table. The behaviour is preserved
  verbatim — every existing test in
  ``tests/unit/test_task_dispatch_binding.py`` /
  ``tests/integration/test_source_scan_concurrency_postgres.py``
  continues to pin the same outcomes.

New code should import directly from :mod:`src.services.dispatch`;
the façade is here only for backward compatibility.
"""

from __future__ import annotations

from src.services.dispatch import (
    TaskCancellationRequested,
    bind_or_create_running_task_log,
    build_dedupe_key,
    create_pending_task_log,
    ensure_dict_detail,
    ensure_task_not_cancelled,
    finalize_cancelled_task_log,
    finalize_task_log,
    find_duplicate_active_task,
    get_task_log_for_update,
    is_singleton_task_running,
    record_deferred_hot_scan,
    renew_task_lease,
    supersede_active_hot_scan,
)

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
    "record_deferred_hot_scan",
    "renew_task_lease",
    "supersede_active_hot_scan",
]
