"""Atomic, auditable state transitions for the offline video pipeline.

Every successful compare-and-set call writes **two** audit records, both
in the same transaction as the business UPDATE:

1. ``TaskLog.detail_json["transition"]`` — kept for backward compat
   with the pre-Todo-17 audit shape, so the inline
   ``detail_json`` payload a worker log always carries still has the
   ``from`` / ``to`` / ``reason`` / ``source`` fields the existing
   tests / dashboards expect.
2. ``PipelineTransitionLog`` row — the new append-only history in
   :mod:`src.models.pipeline_transition_log`. This is the durable
   audit: the row outlives the 7-day ``TaskLog`` cleanup because
   the FK is ``ON DELETE SET NULL`` rather than ``CASCADE`` or
   ``RESTRICT``.

Both writes happen in the caller's session so a single ``commit()``
persists them atomically with the CAS UPDATE itself; a lost race
(CAS matches zero rows) writes **neither**, by construction.

The aggregate-type ↔ status mapping is fixed:

=========================  ======================================================
Aggregate type             ``from_status`` / ``to_status``
=========================  ======================================================
``"VideoSession"``         ``SessionAnalysisStatus`` literal (open / sealed /
                           analyzing / partial / success / failed)
``"TaskLog"``              ``TaskStatus`` literal (pending / running / success /
                           skipped / failed / timeout / cancelled)
=========================  ======================================================

See :mod:`src.models.pipeline_transition_log` for the row contract.
"""

from dataclasses import dataclass
from typing import Final

from sqlalchemy.orm import Session

from src.application.transition_log import (
    AGGREGATE_TYPE_TASK_LOG,
    AGGREGATE_TYPE_VIDEO_SESSION,
)
from src.application.transition_log import (
    record as record_transition_audit,
)
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus

SessionTransition = tuple[SessionAnalysisStatus, SessionAnalysisStatus]
TaskTransition = tuple[TaskStatus, TaskStatus]

SESSION_ALLOWED_TRANSITIONS: Final[frozenset[SessionTransition]] = frozenset(
    {
        (SessionAnalysisStatus.OPEN, SessionAnalysisStatus.SEALED),
        (SessionAnalysisStatus.OPEN, SessionAnalysisStatus.FAILED),
        (SessionAnalysisStatus.SEALED, SessionAnalysisStatus.ANALYZING),
        (SessionAnalysisStatus.PARTIAL, SessionAnalysisStatus.ANALYZING),
        (SessionAnalysisStatus.ANALYZING, SessionAnalysisStatus.SEALED),
        (SessionAnalysisStatus.ANALYZING, SessionAnalysisStatus.PARTIAL),
        (SessionAnalysisStatus.ANALYZING, SessionAnalysisStatus.SUCCESS),
        (SessionAnalysisStatus.ANALYZING, SessionAnalysisStatus.FAILED),
        (SessionAnalysisStatus.PARTIAL, SessionAnalysisStatus.SEALED),
        (SessionAnalysisStatus.FAILED, SessionAnalysisStatus.SEALED),
        (SessionAnalysisStatus.SUCCESS, SessionAnalysisStatus.SEALED),
    }
)

TASK_LOG_ALLOWED_TRANSITIONS: Final[frozenset[TaskTransition]] = frozenset(
    {
        (TaskStatus.PENDING, TaskStatus.RUNNING),
        (TaskStatus.PENDING, TaskStatus.CANCELLED),
        (TaskStatus.RUNNING, TaskStatus.SUCCESS),
        (TaskStatus.RUNNING, TaskStatus.SKIPPED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.TIMEOUT),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
    }
)


@dataclass(frozen=True, slots=True)
class PipelineTransitionConflict(Exception):
    """Raised before a transition that is not in the central state machine."""

    entity: str
    from_status: str
    to_status: str

    def __str__(self) -> str:
        return f"Illegal {self.entity} transition: {self.from_status} -> {self.to_status}"


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Outcome of a conditional state update; ``applied=False`` means lost race."""

    applied: bool


def _record_transition(
    task_log: TaskLog,
    from_status: str,
    to_status: str,
    reason: str,
    source: str,
) -> None:
    detail = dict(task_log.detail_json) if isinstance(task_log.detail_json, dict) else {}
    detail["transition"] = {
        "from": from_status,
        "to": to_status,
        "reason": reason,
        "source": source,
    }
    task_log.detail_json = detail


def transition_session(
    db: Session,
    session_id: int,
    from_status: SessionAnalysisStatus,
    to_status: SessionAnalysisStatus,
    *,
    reason: str,
    source: str,
    task_log: TaskLog | None = None,
    force: bool = False,
) -> TransitionResult:
    """Compare-and-set a session state, the inline audit, and the append-only audit row.

    The CAS UPDATE, the ``task_log.detail_json`` embed, and the
    ``PipelineTransitionLog`` row all sit in the caller's session; a
    single ``commit()`` persists them atomically. A lost race (CAS
    matches zero rows, or the optional ``task_log.cancel_requested``
    short-circuit fires) writes **none** of the three.

    Args:
        db: Caller-owned SQLAlchemy session.
        session_id: ``video_session.id`` to transition.
        from_status: Pre-transition status.
        to_status: Post-transition status. Must be in
            :data:`SESSION_ALLOWED_TRANSITIONS` from ``from_status``,
            else :class:`PipelineTransitionConflict` raises.
        reason: Operator-facing reason (stored on both the inline
            ``detail_json`` embed and the ``pipeline_transition_log``
            row).
        source: Caller identity (function / module name). Required.
        task_log: Optional driving ``TaskLog``. When supplied, the
            inline ``detail_json`` embed is updated so the same row
            that drove the transition still carries a one-step audit;
            the append-only audit row correlates with the row via
            ``task_log_id``.
        force: When ``True`` the ``task_log.cancel_requested``
            short-circuit is bypassed. Reserved for system-initiated
            cancel transitions that need to roll the session back to
            ``SEALED`` despite the driving task log being in
            ``cancel_requested=True`` state.

    Returns:
        :class:`TransitionResult`. ``applied=True`` means the CAS
        UPDATE wrote and both audits landed in the session;
        ``applied=False`` is a lost race that wrote nothing.

    Raises:
        :class:`PipelineTransitionConflict`: when
            ``(from_status, to_status)`` is not in
            :data:`SESSION_ALLOWED_TRANSITIONS`. Checked **before**
            any database work so an illegal transition cannot leave a
            half-written audit row behind.
    """
    if (from_status, to_status) not in SESSION_ALLOWED_TRANSITIONS:
        raise PipelineTransitionConflict("VideoSession", from_status.value, to_status.value)
    if not force and task_log is not None and task_log.cancel_requested:
        return TransitionResult(applied=False)

    updated = (
        db.query(VideoSession)
        .filter(
            VideoSession.id == session_id,
            VideoSession.analysis_status == from_status,
        )
        .update({VideoSession.analysis_status: to_status}, synchronize_session=False)
    )
    if not updated:
        return TransitionResult(applied=False)
    if task_log is not None:
        _record_transition(task_log, from_status.value, to_status.value, reason, source)
    record_transition_audit(
        db,
        aggregate_type=AGGREGATE_TYPE_VIDEO_SESSION,
        aggregate_id=session_id,
        from_status=from_status.value,
        to_status=to_status.value,
        reason=reason,
        source=source,
        task_log_id=task_log.id if task_log is not None else None,
    )
    return TransitionResult(applied=True)


def transition_task_log(
    db: Session,
    task_log_id: int,
    from_status: TaskStatus,
    to_status: TaskStatus,
    *,
    reason: str,
    source: str,
) -> TransitionResult:
    """Compare-and-set a task status, the inline audit, and the append-only audit row.

    Mirrors :func:`transition_session` for the ``TaskLog`` aggregate.
    The single matching ``TaskLog`` row is fetched first so the
    "transition had no driving TaskLog" audit row that follows the
    CAS UPDATE carries the post-transition ``task_log_id``; the same
    row drives the inline ``detail_json`` embed.

    Args:
        db: Caller-owned SQLAlchemy session.
        task_log_id: ``task_log.id`` to transition.
        from_status: Pre-transition status.
        to_status: Post-transition status. Must be in
            :data:`TASK_LOG_ALLOWED_TRANSITIONS` from ``from_status``,
            else :class:`PipelineTransitionConflict` raises.
        reason: Operator-facing reason.
        source: Caller identity.

    Returns:
        :class:`TransitionResult`. ``applied=False`` means the row
        was missing or already in a different ``from_status`` (lost
        race / cancellation / illegal-source) and **no** audit row
        was written.

    Raises:
        :class:`PipelineTransitionConflict`: when
            ``(from_status, to_status)`` is not in
            :data:`TASK_LOG_ALLOWED_TRANSITIONS`.
    """
    if (from_status, to_status) not in TASK_LOG_ALLOWED_TRANSITIONS:
        raise PipelineTransitionConflict("TaskLog", from_status.value, to_status.value)

    task_log = db.query(TaskLog).filter(TaskLog.id == task_log_id).first()
    if task_log is None or task_log.status != from_status:
        return TransitionResult(applied=False)
    updated = (
        db.query(TaskLog)
        .filter(TaskLog.id == task_log_id, TaskLog.status == from_status)
        .update({TaskLog.status: to_status}, synchronize_session=False)
    )
    if not updated:
        return TransitionResult(applied=False)
    db.refresh(task_log)
    _record_transition(task_log, from_status.value, to_status.value, reason, source)
    record_transition_audit(
        db,
        aggregate_type=AGGREGATE_TYPE_TASK_LOG,
        aggregate_id=task_log.id,
        from_status=from_status.value,
        to_status=to_status.value,
        reason=reason,
        source=source,
        task_log_id=task_log.id,
    )
    return TransitionResult(applied=True)
