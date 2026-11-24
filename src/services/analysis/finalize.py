"""Session finalization: aggregation, terminal transitions and webhook hooks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from src.core.contracts import WEBHOOK_SIGNATURE_VERSION
from src.core.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.models.event_record import EventRecord
from src.models.home_profile import HomeProfile
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.webhook_config import WebhookConfig
from src.schemas.home_profile import FocusPointItem, coerce_focus_points
from src.services.analysis.contracts import WEBHOOK_EVENT_SESSION_ANALYZED
from src.services.analysis.execute import ClaimGuards, enforce_fencing
from src.services.attention import attention_keys_for_db, is_attention_event
from src.services.dispatch import create_pending_task_log, finalize_task_log
from src.services.dispatch.outbox import new_task_id, record_delivery
from src.services.dispatch.task_command import TaskCommand
from src.services.pipeline_state import transition_session, transition_task_log
from src.services.video_analysis.mapper import build_event_record_from_recognized_event
from src.services.video_analysis.schemas import RecognitionResultDTO
from src.services.webhook_payload import build_webhook_event_payload
from src.services.webhook_subscription import webhook_subscribes


# --- aggregator.py ---
def _profile_focus_items(db: Session) -> list[FocusPointItem]:
    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    return coerce_focus_points(profile.focus_points_json) if profile else []


# --- aggregator.py ---
def _declared_focus_keys(db: Session) -> set[str]:
    return {item.key for item in _profile_focus_items(db) if item.enabled}


# --- aggregator.py ---
def _is_attention(event: EventRecord, attention_keys: frozenset[str]) -> bool:
    return is_attention_event(
        event_type=event.event_type,
        related_entities=event.related_entities_json,
        focus_matches=event.focus_matches_json,
        attention_keys=attention_keys,
    )


# --- aggregator.py ---
def completed_results_for_run(
    db: Session, session: VideoSession, analysis_run_id: str
) -> tuple[list[tuple[int, int, RecognitionResultDTO]], list[EventRecord]]:
    """Read the success checkpoints and rehydrate the per-sub-chunk results."""
    checkpoints = (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.session_id == session.id,
            SessionAnalysisCheckpoint.analysis_run_id == analysis_run_id,
            SessionAnalysisCheckpoint.state == "success",
        )
        .order_by(
            SessionAnalysisCheckpoint.chunk_index,
            SessionAnalysisCheckpoint.sub_chunk_index,
        )
        .all()
    )
    results: list[tuple[int, int, RecognitionResultDTO]] = []
    events: list[EventRecord] = []
    allowed_focus_keys = _declared_focus_keys(db)
    for checkpoint in checkpoints:
        if checkpoint.event_payload is None:
            continue
        result = RecognitionResultDTO.model_validate(checkpoint.event_payload)
        results.append((checkpoint.chunk_index, checkpoint.sub_chunk_index, result))
        for item in result.events:
            event = build_event_record_from_recognized_event(
                session, item, base_offset_seconds=checkpoint.start_offset_seconds
            )
            event.focus_matches_json = [
                key for key in (event.focus_matches_json or []) if key in allowed_focus_keys
            ]
            events.append(event)
    return results, events


# --- aggregator.py ---
def merge_session_fields(
    session: VideoSession,
    structured_results: list[tuple[int, int, RecognitionResultDTO]],
    events: list[EventRecord],
    *,
    attention_keys: frozenset[str] = frozenset(),
) -> None:
    """Fold per-sub-chunk summaries into the session-level projection."""
    has_attention_event = any(_is_attention(event, attention_keys) for event in events)
    if not structured_results:
        session.summary_text = (
            f"分段识别完成，共识别 {len(events)} 个事件" if events else "未识别到有效事件"
        )
        session.activity_level = "medium" if events else "low"
        session.main_subjects_json = []
        session.has_attention_event = has_attention_event
        session.analysis_notes_json = []
        return

    summary_lines: list[str] = []
    if len(structured_results) == 1:
        summary_lines.append(structured_results[0][2].session_summary.summary_text)
    else:
        for _chunk_index, sub_index, result in structured_results:
            summary_lines.append(f"文件{sub_index + 1}: {result.session_summary.summary_text}")

    activity_score = {"low": 1, "medium": 2, "high": 3}
    highest_activity = "low"
    subjects_seen: set[str] = set()
    merged_subjects: list[str] = []
    merged_notes: list[dict[str, Any]] = []
    notes_seen: set[tuple[Any, ...]] = set()

    for sub_chunk_index, _, result in structured_results:
        summary = result.session_summary
        if activity_score[summary.activity_level] > activity_score[highest_activity]:
            highest_activity = summary.activity_level

        for subject in summary.main_subjects:
            key = subject.strip()
            if not key or key in subjects_seen:
                continue
            subjects_seen.add(key)
            merged_subjects.append(key)

        for note in result.analysis_notes:
            note_key = (
                note.type,
                note.note.strip(),
                note.entity_index,
                tuple(note.candidates),
            )
            if not note_key[1] or note_key in notes_seen:
                continue
            notes_seen.add(note_key)
            merged_notes.append(
                {
                    "type": note.type,
                    "note": note.note,
                    "entity_index": note.entity_index,
                    "candidates": list(note.candidates),
                    "sub_chunk_index": sub_chunk_index,
                }
            )

    session.summary_text = "\n".join(summary_lines)
    session.activity_level = highest_activity
    session.main_subjects_json = merged_subjects
    session.has_attention_event = has_attention_event
    session.analysis_notes_json = merged_notes


# --- aggregator.py ---
def replace_session_events(db: Session, session_id: int, events: list[EventRecord]) -> int:
    """Atomic delete-then-insert for the session's event rows."""
    deleted_count = (
        db.query(EventRecord)
        .filter(EventRecord.session_id == session_id)
        .delete(synchronize_session=False)
    )
    for event in events:
        db.add(event)
    return int(deleted_count or 0)


# --- aggregator.py ---
_replace_session_events = replace_session_events


# --- finalize.py ---
def finalize_session_success(
    db: Session,
    *,
    claim: ClaimGuards,
    session: VideoSession,
    task_log: TaskLog,
    analysis_run_id: str,
    file_count: int,
    sub_chunk_count: int,
    priority: str,
    raw_mp4_num_frames: int,
    parse_modes: list[str],
    media_input_budget: int | None = None,
    deadline_prompts: list[str] | None = None,
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
    merge_session_fields(
        session, structured_results, events_to_persist, attention_keys=attention_keys_for_db(db)
    )
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
    subscribers = find_session_analyzed_subscribers(db)
    finalize_task_log(
        db,
        task_log,
        TaskStatus.SUCCESS,
        f"Analyzed session across {file_count} files, created {len(events_to_persist)} events.",
        {
            "session_id": session.id,
            "events_created": len(events_to_persist),
            "events_replaced_deleted": replaced_deleted_count,
            "file_count": file_count,
            "recognized_sub_chunks": sub_chunk_count,
            "media_num_frames": raw_mp4_num_frames,
            "media_input_budget": media_input_budget,
            "parse_modes": parse_modes,
            "priority": priority,
            "webhook_subscribers": len(subscribers),
        },
    )
    if subscribers:
        enroll_session_analyzed_webhooks(
            db,
            session_id=session.id,
            source_id=session.source_id,
            events_created=len(events_to_persist),
            file_count=file_count,
            analysis_status=SessionAnalysisStatus.SUCCESS.value,
            subscribers=subscribers,
        )
    db.commit()
    return {
        "events_created": len(events_to_persist),
        "file_count": file_count,
        "recognized_sub_chunks": sub_chunk_count,
    }


# --- finalize.py ---
def _skip_response(session_id: int, reason: str, priority: str) -> dict[str, Any]:
    return {
        "events_created": 0,
        "skipped": True,
        "reason": reason,
        "session_id": session_id,
        "priority": priority,
    }


# --- finalize.py ---
def cancel_session(
    db: Session,
    *,
    task_log_id: int,
    session_id: int,
    priority: str,
    last_chunk_index: int | None,
    last_sub_chunk_index: int | None,
    message: str,
) -> dict[str, Any]:
    """Roll an interrupted run back to SEALED and finalize the TaskLog as CANCELLED."""
    from src.services.dispatch import finalize_cancelled_task_log

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
        db,
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


# --- finalize.py ---
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


# --- finalize.py ---
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


# --- webhook_events.py ---
WEBHOOK_TASK_NAME: str = "src.tasks.webhook.send_webhook_task"


# --- webhook_events.py ---
DEFAULT_QUEUE: str = "celery"


# --- webhook_events.py ---
SUBSCRIBED_VERSION: str = WEBHOOK_SIGNATURE_VERSION


# --- webhook_events.py ---
def find_session_analyzed_subscribers(db: Session) -> list[int]:
    """Return enabled webhook ids subscribed to ``session_analyzed``."""
    hooks = db.query(WebhookConfig).filter(WebhookConfig.enabled.is_(True)).all()
    return [
        int(hook.id)
        for hook in hooks
        if webhook_subscribes(
            hook,
            event_type=WEBHOOK_EVENT_SESSION_ANALYZED,
            version=SUBSCRIBED_VERSION,
        )
    ]


# --- webhook_events.py ---
def enroll_session_analyzed_webhooks(
    db: Session,
    *,
    session_id: int,
    source_id: int,
    events_created: int,
    file_count: int,
    analysis_status: str,
    subscribers: list[int],
) -> list[UUID]:
    """Enroll one task log + outbox row per subscriber in the caller's tx."""
    enrolled: list[UUID] = []
    generated_at = datetime.now(timezone.utc)
    for webhook_id in subscribers:
        task_log, _created = create_pending_task_log(
            db,
            task_type=TaskType.WEBHOOK_PUSH,
            task_target_id=int(webhook_id),
            detail_json={
                "event_type": WEBHOOK_EVENT_SESSION_ANALYZED,
                "webhook_id": int(webhook_id),
                "session_id": session_id,
            },
        )
        payload: dict[str, Any] = build_webhook_event_payload(
            WEBHOOK_EVENT_SESSION_ANALYZED,
            {
                "session_id": session_id,
                "source_id": source_id,
                "events_created": events_created,
                "file_count": file_count,
                "analysis_status": analysis_status,
                "webhook_id": int(webhook_id),
            },
            generated_at=generated_at,
        )
        command = TaskCommand(
            task_name=WEBHOOK_TASK_NAME,
            queue=DEFAULT_QUEUE,
            kwargs={
                "event_type": WEBHOOK_EVENT_SESSION_ANALYZED,
                "webhook_id": int(webhook_id),
                "payload": payload,
            },
        )
        task_id = new_task_id()
        record_delivery(db, task_log, command, task_id=task_id)
        enrolled.append(UUID(task_id))
    return enrolled


__all__ = [
    "attention_keys_for_db",
    "cancel_session",
    "completed_results_for_run",
    "enroll_session_analyzed_webhooks",
    "finalize_session_success",
    "find_session_analyzed_subscribers",
    "merge_session_fields",
    "record_failed_transition",
    "record_partial_failure_transition",
    "replace_session_events",
]
