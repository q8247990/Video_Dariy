"""Aggregator: fold per-sub-chunk recognition results into session fields.

Given the ``state=success`` checkpoints for the current
``analysis_run_id``, build the canonical :class:`EventRecord` list
and the merged ``VideoSession.summary_text`` /
``activity_level`` / ``main_subjects_json`` /
``has_important_event`` / ``analysis_notes_json`` projections.

The replace step (``replace_session_events``) and the merge step
(``merge_session_fields``) are split so the slim Celery task can
call them independently and so unit tests can pin each behaviour
separately. The ``completed_results_for_run`` helper is the read
path: it scans the checkpoints in ``(chunk_index, sub_chunk_index)``
order and re-builds the recognition DTO + EventRecord list, which
the aggregator then folds into session fields.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.models.event_record import EventRecord
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.video_session import VideoSession
from src.services.video_analysis.mapper import build_event_record_from_recognized_event
from src.services.video_analysis.schemas import RecognitionResultDTO


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
    for checkpoint in checkpoints:
        if checkpoint.event_payload is None:
            continue
        result = RecognitionResultDTO.model_validate(checkpoint.event_payload)
        results.append((checkpoint.chunk_index, checkpoint.sub_chunk_index, result))
        for item in result.events:
            events.append(
                build_event_record_from_recognized_event(
                    session, item, base_offset_seconds=checkpoint.start_offset_seconds
                )
            )
    return results, events


def merge_session_fields(
    session: VideoSession,
    structured_results: list[tuple[int, int, RecognitionResultDTO]],
    events: list[EventRecord],
) -> None:
    """Fold per-sub-chunk summaries into the session-level projection."""
    if not structured_results:
        session.summary_text = (
            f"分段识别完成，共识别 {len(events)} 个事件" if events else "未识别到有效事件"
        )
        session.activity_level = "medium" if events else "low"
        session.main_subjects_json = []
        session.has_important_event = any(
            event.importance_level in {"high", "medium"} for event in events
        )
        session.analysis_notes_json = []
        return

    summary_lines: list[str] = []
    if len(structured_results) == 1:
        summary_lines.append(structured_results[0][2].session_summary.summary_text)
    else:
        for chunk_index, sub_index, result in structured_results:
            summary_lines.append(
                f"片段{chunk_index + 1}-{sub_index + 1}: {result.session_summary.summary_text}"
            )

    activity_score = {"low": 1, "medium": 2, "high": 3}
    highest_activity = "low"
    subjects_seen: set[str] = set()
    merged_subjects: list[str] = []
    merged_notes: list[dict[str, str]] = []
    notes_seen: set[tuple[str, str]] = set()
    has_important_event = any(event.importance_level in {"high", "medium"} for event in events)

    for _, _, result in structured_results:
        summary = result.session_summary
        if activity_score[summary.activity_level] > activity_score[highest_activity]:
            highest_activity = summary.activity_level
        has_important_event = has_important_event or summary.has_important_event

        for subject in summary.main_subjects:
            key = subject.strip()
            if not key or key in subjects_seen:
                continue
            subjects_seen.add(key)
            merged_subjects.append(key)

        for note in result.analysis_notes:
            note_key = (note.type, note.note.strip())
            if not note_key[1] or note_key in notes_seen:
                continue
            notes_seen.add(note_key)
            merged_notes.append({"type": note.type, "note": note.note})

    session.summary_text = "\n".join(summary_lines)
    session.activity_level = highest_activity
    session.main_subjects_json = merged_subjects
    session.has_important_event = has_important_event
    session.analysis_notes_json = merged_notes


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


__all__ = [
    "completed_results_for_run",
    "merge_session_fields",
    "replace_session_events",
]


# Backward-compat aliases used by the existing
# ``tests/unit/test_analyzer_task.py`` test which monkeypatches
# ``src.tasks.analyzer._replace_session_events``.
_replace_session_events = replace_session_events
