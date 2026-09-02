"""Append-only audit for the pipeline state machine.

This package owns the SQLAlchemy bridge that persists every
successful ``pipeline_state`` CAS transition as an
append-only row in :class:`src.models.pipeline_transition_log.PipelineTransitionLog`.
The history table exists because ``task_log`` is pruned after 7
days (``src/tasks/task_maintenance.py:_cleanup_old_task_logs``) and
``detail_json`` lives on the very ``TaskLog`` row whose mutation made
the row interesting — both of those concerns disappear if the audit
lives on a separate table with a nullable correlation FK.

Submodules
==========

``repository``
    Single function :func:`record` — the **only** writer in the
    application. Appends one row to the caller's session without
    committing, so the audit INSERT shares the caller's transaction
    with the CAS UPDATE that drove it. A pre-INSERT contract check
    keeps ``aggregate_type`` literal typos loud (a ``ValueError``
    here, rather than a low-signal ``IntegrityError`` from the CHECK
    constraint at the DB layer).
"""

from __future__ import annotations

from src.application.transition_log.repository import (
    AGGREGATE_TYPE_TASK_LOG,
    AGGREGATE_TYPE_VIDEO_SESSION,
    record,
)

__all__ = [
    "AGGREGATE_TYPE_TASK_LOG",
    "AGGREGATE_TYPE_VIDEO_SESSION",
    "record",
]
