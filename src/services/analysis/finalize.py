"""Canonical finalize stage.

Composed of three short transactions that each carry a fencing
guard:

1. Read the success checkpoints for the current ``analysis_run_id``
   and aggregate them into session-level fields
   (:func:`aggregator.merge_session_fields`).
2. Replace the session's ``EventRecord`` rows atomically
   (:func:`aggregator.replace_session_events`).
3. Transition ``VideoSession`` to ``SUCCESS`` (or ``PARTIAL`` if
   some sub-chunks failed) and finalize the TaskLog.

A late worker (whose claim no longer matches the live TaskLog) is
caught by :func:`checkpoint_writer.enforce_fencing` before any of
these transactions run, so a worker hand-off cannot leave the
session in a half-finalized state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.services.analysis.aggregator import (
    completed_results_for_run,
    merge_session_fields,
    replace_session_events,
)
from src.services.analysis.checkpoint_failure import enforce_fencing
from src.services.analysis.checkpoint_writer import ClaimGuards
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
)
from src.services.pipeline_state import transition_session, transition_task_log
from src.services.task_dispatch_control import finalize_task_log


def finalize_session_success(  # noqa: PLR0913 — mirrors the legacy finalize dict shape.
    db: Session,
    *,
    claim: ClaimGuards,
    session: VideoSession,
    task_log: TaskLog,
    analysis_run_id: str,
    chunk_count: int,
    sub_chunk_count: int,
    priority: str,
    raw_mp4_num_frames: int,
    chunk_seconds: int,
    sub_chunk_seconds: int,
    parse_modes: list[str],
    deadline_prompts: Optional[list[str]] = None,
    replace_session_events_fn: Any = None,
) -> dict[str, Any]:
    """Fold checkpoints into session fields, replace events, transition to SUCCESS."""
    del deadline_prompts
    enforce_fencing(db, claim=claim)
    # Expire cached session fields so ``transition_session``'s CAS
    # UPDATE sees the fresh ``analysis_status`` value — the
    # ``synchronize_session=False`` flag on the CAS UPDATE leaves the
    # in-memory copy stale otherwise.
    db.expire(session)
    structured_results, events_to_persist = completed_results_for_run(db, session, analysis_run_id)
    merge_session_fields(session, structured_results, events_to_persist)
    if replace_session_events_fn is None:
        replace_session_events_fn = replace_session_events
    replaced_deleted_count = replace_session_events_fn(db, session.id, events_to_persist)
    completed = transition_session(
        db,
        session.id,
        SessionAnalysisStatus.ANALYZING,
        SessionAnalysisStatus.SUCCESS,
        reason="analysis_completed",
        source="analyze_session_task",
        task_log=task_log,
    )
    if not completed.applied:
        return _skip_response(session.id, "completion_transition_conflict", priority)

    session.last_analyzed_at = datetime.now(timezone.utc)
    transition_task_log(
        db,
        task_log.id,
        TaskStatus.RUNNING,
        TaskStatus.SUCCESS,
        reason="analysis_completed",
        source="analyze_session_task",
    )
    finalize_task_log(
        task_log,
        TaskStatus.SUCCESS,
        f"Analyzed session in {chunk_count} chunks / "
        f"{sub_chunk_count} sub-chunks, created {len(events_to_persist)} events.",
        {
            "session_id": session.id,
            "events_created": len(events_to_persist),
            "events_replaced_deleted": replaced_deleted_count,
            "chunk_count": chunk_count,
            "sub_chunk_count": sub_chunk_count,
            "chunk_seconds": chunk_seconds,
            "llm_chunk_seconds": sub_chunk_seconds,
            "media_num_frames": raw_mp4_num_frames,
            "parse_modes": parse_modes,
            "priority": priority,
        },
    )
    db.commit()
    return {
        "events_created": len(events_to_persist),
        "chunk_count": chunk_count,
        "sub_chunk_count": sub_chunk_count,
    }


def _skip_response(session_id: int, reason: str, priority: str) -> dict[str, Any]:
    return {
        "events_created": 0,
        "skipped": True,
        "reason": reason,
        "session_id": session_id,
        "priority": priority,
    }


def cancel_session(  # noqa: PLR0913 — mirrors the legacy cancel dict shape.
    db: Session,
    *,
    task_log_id: int,
    session_id: int,
    priority: str,
    last_chunk_index: Optional[int],
    last_sub_chunk_index: Optional[int],
    message: str,
) -> dict[str, Any]:
    """Roll an interrupted run back to SEALED and finalize the TaskLog as CANCELLED."""
    from src.services.task_dispatch_control import finalize_cancelled_task_log

    task_log = db.query(TaskLog).filter(TaskLog.id == task_log_id).first()
    if task_log is None:
        raise RuntimeError(f"TaskLog {task_log_id} disappeared during cancel")
    session = db.query(VideoSession).filter(VideoSession.id == session_id).one()
    db.refresh(session)
    if session.analysis_status == SessionAnalysisStatus.ANALYZING.value:
        transition_session(
            db,
            session.id,
            SessionAnalysisStatus.ANALYZING,
            SessionAnalysisStatus.SEALED,
            reason="analysis_cancelled",
            source="analyze_session_task",
            task_log=task_log,
            force=True,
        )
    transition_task_log(
        db,
        task_log_id,
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
        reason="analysis_cancelled",
        source="analyze_session_task",
    )
    finalize_cancelled_task_log(
        task_log,
        message,
        {
            "session_id": session_id,
            "cancelled": True,
            "failed_chunk_index": last_chunk_index,
            "failed_sub_chunk_index": last_sub_chunk_index,
            "priority": priority,
        },
    )
    db.commit()
    return {
        "cancelled": True,
        "session_id": session_id,
        "chunk_index": last_chunk_index,
        "sub_chunk_index": last_sub_chunk_index,
    }


def record_partial_failure_transition(
    db: Session,
    *,
    session: VideoSession,
    task_log: TaskLog,
) -> None:
    """Mark the session PARTIAL after at least one sub-chunk committed."""
    transition_session(
        db,
        session.id,
        SessionAnalysisStatus.ANALYZING,
        SessionAnalysisStatus.PARTIAL,
        reason="analysis_failed",
        source="analyze_session_task",
        task_log=task_log,
    )


def record_failed_transition(
    db: Session,
    *,
    session: VideoSession,
    task_log: TaskLog,
) -> None:
    """Mark the session FAILED when no sub-chunk committed."""
    transition_session(
        db,
        session.id,
        SessionAnalysisStatus.ANALYZING,
        SessionAnalysisStatus.FAILED,
        reason="analysis_failed",
        source="analyze_session_task",
        task_log=task_log,
    )


__all__ = [
    "cancel_session",
    "finalize_session_success",
    "record_failed_transition",
    "record_partial_failure_transition",
]
