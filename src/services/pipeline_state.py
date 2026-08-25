"""Atomic, auditable state transitions for the offline video pipeline."""

from dataclasses import dataclass
from typing import Final

from sqlalchemy.orm import Session

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
) -> TransitionResult:
    """Compare-and-set a session state and append audit metadata to its task log."""
    if (from_status, to_status) not in SESSION_ALLOWED_TRANSITIONS:
        raise PipelineTransitionConflict("VideoSession", from_status.value, to_status.value)
    if task_log is not None and task_log.cancel_requested:
        return TransitionResult(applied=False)

    updated = (
        db.query(VideoSession)
        .filter(
            VideoSession.id == session_id,
            VideoSession.analysis_status == from_status,
        )
        .update({VideoSession.analysis_status: to_status}, synchronize_session=False)
    )
    if updated and task_log is not None:
        _record_transition(task_log, from_status.value, to_status.value, reason, source)
    return TransitionResult(applied=bool(updated))


def transition_task_log(
    db: Session,
    task_log_id: int,
    from_status: TaskStatus,
    to_status: TaskStatus,
    *,
    reason: str,
    source: str,
) -> TransitionResult:
    """Compare-and-set a task status, retaining a compact audit trail in detail JSON."""
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
    if updated:
        db.refresh(task_log)
        _record_transition(task_log, from_status.value, to_status.value, reason, source)
    return TransitionResult(applied=bool(updated))
