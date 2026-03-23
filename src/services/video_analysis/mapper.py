from datetime import timedelta

from src.models.event_record import EventRecord
from src.models.video_session import VideoSession
from src.services.video_analysis.schemas import RecognitionResultDTO, RecognizedEventDTO


def map_recognition_result_to_session(session: VideoSession, result: RecognitionResultDTO) -> None:
    session.summary_text = result.session_summary.summary_text
    session.activity_level = result.session_summary.activity_level
    session.main_subjects_json = result.session_summary.main_subjects
    session.has_important_event = result.session_summary.has_important_event
    session.analysis_notes_json = [note.model_dump() for note in result.analysis_notes]


def build_event_record_from_recognized_event(
    session: VideoSession,
    recognized_event: RecognizedEventDTO,
    base_offset_seconds: float = 0,
) -> EventRecord:
    absolute_offset_start = base_offset_seconds + recognized_event.offset_start_sec
    absolute_offset_end = base_offset_seconds + recognized_event.offset_end_sec
    start_time = session.session_start_time + timedelta(seconds=absolute_offset_start)
    end_time = session.session_start_time + timedelta(seconds=absolute_offset_end)
    return EventRecord(
        source_id=session.source_id,
        session_id=session.id,
        event_start_time=start_time,
        event_end_time=end_time,
        object_type=(
            recognized_event.related_entities[0].entity_type
            if recognized_event.related_entities
            else None
        ),
        action_type=recognized_event.event_type,
        description=recognized_event.summary,
        confidence_score=recognized_event.confidence,
        raw_result=recognized_event.model_dump(),
        event_type=recognized_event.event_type,
        title=recognized_event.title,
        summary=recognized_event.summary,
        detail=recognized_event.detail,
        importance_level=recognized_event.importance_level,
        offset_start_sec=absolute_offset_start,
        offset_end_sec=absolute_offset_end,
        related_entities_json=[item.model_dump() for item in recognized_event.related_entities],
        observed_actions_json=recognized_event.observed_actions,
        interpreted_state_json=recognized_event.interpreted_state,
    )
