from datetime import datetime

from src.models.video_session import VideoSession
from src.services.video_analysis.mapper import build_event_record_from_recognized_event
from src.services.video_analysis.schemas import RecognizedEventDTO


def test_build_event_record_with_base_offset() -> None:
    session = VideoSession(
        id=7,
        source_id=3,
        session_start_time=datetime(2026, 3, 14, 10, 0, 0),
        session_end_time=datetime(2026, 3, 14, 10, 30, 0),
        total_duration_seconds=1800,
        analysis_status="pending",
    )

    recognized_event = RecognizedEventDTO(
        offset_start_sec=10,
        offset_end_sec=22,
        event_type="member_appear",
        title="成员出现",
        summary="成员进入客厅",
        detail="成员从门口进入，经过茶几后在沙发附近停留。",
        related_entities=[],
        observed_actions=["walk"],
        interpreted_state=["normal"],
        confidence=0.95,
        importance_level="medium",
    )

    event = build_event_record_from_recognized_event(
        session,
        recognized_event,
        base_offset_seconds=600,
    )

    assert float(event.offset_start_sec) == 610.0
    assert float(event.offset_end_sec) == 622.0
    assert event.event_start_time.isoformat() == "2026-03-14T10:10:10"
    assert event.event_end_time.isoformat() == "2026-03-14T10:10:22"
    assert event.detail == "成员从门口进入，经过茶几后在沙发附近停留。"
