"""Shared constants for the dispatch-control policy modules.

The dispatch package splits the pre-Wave-5 monolithic
``src/services/task_dispatch_control.py`` (415 LOC) into discrete
policy modules — :mod:`.dedupe`, :mod:`.claim`, :mod:`.worker_bind`,
:mod:`.lease`, :mod:`.finalize`, :mod:`.cancel`, :mod:`.deferred`.

This module owns the constants that several policies share so each
policy module stays free of duplicated enum literals and the
:data:`TERMINAL_TASK_STATUSES` tuple is computed once.

The terminal status set mirrors :class:`src.services.pipeline_constants.TaskStatus`
and is the canonical "no further transitions allowed" set used by
both the lease verification helpers and the worker-bind stale-message
short-circuit. It must stay in lock-step with
:data:`src.services.pipeline_state.TASK_LOG_ALLOWED_TRANSITIONS`:
every status the state-machine refuses to leave is terminal.
"""

from __future__ import annotations

from src.services.pipeline_constants import TaskStatus

TERMINAL_TASK_STATUSES: tuple[str, ...] = (
    TaskStatus.SUCCESS,
    TaskStatus.SKIPPED,
    TaskStatus.FAILED,
    TaskStatus.TIMEOUT,
    TaskStatus.CANCELLED,
)


__all__ = ["TERMINAL_TASK_STATUSES"]
