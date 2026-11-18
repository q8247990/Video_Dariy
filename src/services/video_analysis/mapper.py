from datetime import timedelta

from src.models.event_record import EventRecord
from src.models.video_session import VideoSession
from src.services.video_analysis.output_parser import RecognitionOutputValidationError
from src.services.video_analysis.schemas import RecognitionResultDTO, RecognizedEventDTO


def validate_recognition_offsets_within_sub_chunk(
    result: RecognitionResultDTO,
    *,
    sub_chunk_duration_seconds: float,
) -> None:
    """Reject events whose offsets fall outside the sub-chunk window.

    The parser only knows the per-event invariant
    (``offset_end_sec >= offset_start_sec >= 0``); the sub-chunk boundary
    is only known to the caller that owns the plan item, so the guard is
    applied right after parsing — before the checkpoint is persisted. An
    out-of-window offset means the model hallucinated a timestamp beyond
    the material it was given (a frame/clip of exactly
    ``sub_chunk_duration_seconds``), which would silently shift the event
    into the next file's time range and break the absolute-time contract
    that daily summary / QA / MCP consume.

    Raises :class:`RecognitionOutputValidationError` (a ``ValueError``)
    naming the offending offset and duration, so the failing sub-chunk
    lands in the standard PARTIAL/FAILED handling. No public schema is
    changed.
    """
    for index, event in enumerate(result.events):
        if event.offset_end_sec > sub_chunk_duration_seconds:
            raise RecognitionOutputValidationError(
                f"event #{index} offset_end_sec={event.offset_end_sec}s exceeds "
                f"sub-chunk duration_seconds={sub_chunk_duration_seconds}s"
            )


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
        offset_start_sec=absolute_offset_start,
        offset_end_sec=absolute_offset_end,
        related_entities_json=[item.model_dump() for item in recognized_event.related_entities],
        observed_actions_json=recognized_event.observed_actions,
        interpreted_state_json=recognized_event.interpreted_state,
        focus_matches_json=recognized_event.focus_matches,
    )
