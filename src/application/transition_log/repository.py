"""Append-only writer for the pipeline transition audit.

This module owns the **persistence-side** mechanics for
:class:`src.models.pipeline_transition_log.PipelineTransitionLog`.
It is the single writer — :func:`record` — and exists separately from
the state-machine helpers in
:mod:`src.services.pipeline_state` so the CAS logic and the audit
write can stay independent of each other.

Atomicity contract
==================

:func:`record` does **not** open a new transaction. It adds the audit
row to the caller-supplied ``Session`` and relies on the caller to
``commit()`` for atomicity. The state-machine helpers in
:mod:`src.services.pipeline_state` go one step further: their CAS
``UPDATE`` *and* this audit INSERT sit in the same caller's session,
so a single ``commit()`` persists them atomically. If the CAS
condition matches zero rows (a lost race / illegal transition /
cancelled TaskLog), this function is never called and no audit row
is written — the row count must match the transition count, by
construction.

Two-state ``aggregate_type`` guarantee
======================================

The CHECK constraint on the
``pipeline_transition_log.aggregate_type`` column pins the two legal
literals (``"VideoSession"`` and ``"TaskLog"``). The application
writer is responsible for keeping the call-site literal in sync with
that CHECK; callers that skip this helper risk an
``IntegrityError`` at INSERT time.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from src.models.pipeline_transition_log import PipelineTransitionLog

#: Legal ``aggregate_type`` literals. Kept in one place so the
#: application-side call sites and the CHECK constraint (see the
#: ``pipeline_transition_log`` migration) cannot drift silently.
AGGREGATE_TYPE_VIDEO_SESSION = "VideoSession"
AGGREGATE_TYPE_TASK_LOG = "TaskLog"

_ALLOWED_AGGREGATE_TYPES: frozenset[str] = frozenset(
    {AGGREGATE_TYPE_VIDEO_SESSION, AGGREGATE_TYPE_TASK_LOG}
)


def record(
    db_session: Session,
    *,
    aggregate_type: str,
    aggregate_id: int,
    from_status: str,
    to_status: str,
    reason: Optional[str],
    source: str,
    task_log_id: Optional[int] = None,
) -> None:
    """Append a single audit row to the caller's session.

    The row is added to ``db_session`` but **not** committed; the caller
    owns the transaction so the audit INSERT shares one commit with
    the business write that drove it (typically the CAS UPDATE in
    :func:`src.services.pipeline_state.transition_session` /
    :func:`src.services.pipeline_state.transition_task_log`).

    Args:
        db_session: The active SQLAlchemy session. Owned by the caller.
        aggregate_type: One of ``"VideoSession"`` /
            ``"TaskLog"``. Passing an unknown value raises
            ``ValueError``; doing so here (before the INSERT) keeps a
            typo on the call site loud rather than failing in the DB
            with a less useful ``IntegrityError``.
        aggregate_id: PK value of the aggregate row (``video_session.id``
            or ``task_log.id``).
        from_status: Pre-transition status literal, as written by the
            state-machine helpers (e.g. ``"analyzing"``).
        to_status: Post-transition status literal.
        reason: Short operator / system reason. ``None`` allowed.
        source: Caller identity (function or module name). Required.
        task_log_id: Optional FK to ``task_log.id``. ``None`` when the
            transition had no driving TaskLog (e.g. a session
            transition during scan completion that runs without an
            active TaskLog).

    Raises:
        ValueError: ``aggregate_type`` is not one of the two legal
            literals.
    """
    if aggregate_type not in _ALLOWED_AGGREGATE_TYPES:
        raise ValueError(
            f"aggregate_type must be one of {sorted(_ALLOWED_AGGREGATE_TYPES)}, "
            f"got {aggregate_type!r}"
        )
    if not source:
        raise ValueError("source is required for pipeline_transition_log audit rows")

    row = PipelineTransitionLog(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        source=source,
        task_log_id=task_log_id,
    )
    db_session.add(row)


__all__ = [
    "AGGREGATE_TYPE_TASK_LOG",
    "AGGREGATE_TYPE_VIDEO_SESSION",
    "record",
]
