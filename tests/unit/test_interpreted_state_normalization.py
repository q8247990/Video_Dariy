from datetime import datetime
from types import SimpleNamespace

from src.models.video_session import VideoSession
from src.services.analysis.finalize import merge_session_fields
from src.services.video_analysis.enums import normalize_interpreted_state
from src.services.video_analysis.mapper import build_event_record_from_recognized_event
from src.services.video_analysis.schemas import (
    AnalysisNoteDTO,
    RecognitionResultDTO,
    RecognizedEventDTO,
    RelatedEntityDTO,
    SessionSummaryDTO,
)


def test_normalize_interpreted_state_collapses_synonyms_in_order() -> None:
    assert normalize_interpreted_state(["移动中", "离场", "静止", "移动"]) == [
        "移动",
        "离开",
        "静止",
    ]


def test_normalize_interpreted_state_drops_unknown_and_falls_back() -> None:
    assert normalize_interpreted_state(["整理物品", "unknown"]) == ["在场"]
    assert normalize_interpreted_state([]) == []
    assert normalize_interpreted_state(None) == []


def test_mapper_persists_normalized_state_and_candidates() -> None:
    session = SimpleNamespace(
        id=7,
        source_id=3,
        session_start_time=datetime(2026, 11, 24, 12, 0, 0),
    )
    event = RecognizedEventDTO(
        offset_start_sec=0,
        offset_end_sec=10,
        event_type="pet_stay",
        title="深色猫在沙发附近停留",
        summary="深色猫在沙发附近停留。",
        detail="画面左侧，深色猫趴在沙发边缘。",
        related_entities=[
            RelatedEntityDTO(
                entity_type="pet",
                display_name="深色猫",
                matched_profile_name=None,
                candidate_profile_names=["墨墨", "嘟嘟", "墨墨"],
                recognition_status="suspected",
                confidence=0.5,
            )
        ],
        interpreted_state=["趴着", "移动中"],
        confidence=0.6,
    )

    record = build_event_record_from_recognized_event(session, event)

    assert record.interpreted_state_json == ["休息", "移动"]
    assert record.related_entities_json[0]["candidate_profile_names"] == ["墨墨", "嘟嘟"]


def test_analysis_note_carries_structured_candidates() -> None:
    result = RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text="画面无明显变化。",
            activity_level="low",
        ),
        events=[],
        analysis_notes=[
            AnalysisNoteDTO(
                type="identity_uncertain",
                note="深色猫无法确认身份。",
                entity_index=1,
                candidates=["墨墨", "嘟嘟"],
            )
        ],
    )

    note = result.analysis_notes[0]
    assert note.entity_index == 1
    assert note.candidates == ["墨墨", "嘟嘟"]


def test_merge_session_fields_keeps_structured_identity_notes() -> None:
    session = VideoSession(
        source_id=1,
        session_start_time=datetime(2026, 11, 24, 12, 0, 0),
        session_end_time=datetime(2026, 11, 24, 12, 1, 0),
        analysis_status="analyzing",
    )
    result = RecognitionResultDTO(
        session_summary=SessionSummaryDTO(summary_text="深色猫在沙发附近。", activity_level="low"),
        events=[],
        analysis_notes=[
            AnalysisNoteDTO(
                type="identity_uncertain",
                note="深色猫无法确认身份。",
                entity_index=1,
                candidates=["墨墨", "嘟嘟"],
            )
        ],
    )

    merge_session_fields(session, [(0, 0, result)], [])

    assert session.analysis_notes_json == [
        {
            "type": "identity_uncertain",
            "note": "深色猫无法确认身份。",
            "entity_index": 1,
            "candidates": ["墨墨", "嘟嘟"],
            "sub_chunk_index": 0,
        }
    ]
