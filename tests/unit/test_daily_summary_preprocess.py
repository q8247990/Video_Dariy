from datetime import datetime

from src.models.event_record import EventRecord
from src.services.daily_summary.preprocess import (
    build_known_subjects,
    build_subject_event_mapping,
    extract_attention_candidates,
)


def test_build_subject_event_mapping_and_missing_subjects() -> None:
    home_context = {
        "members": [{"name": "爸爸"}],
        "pets": [{"name": "布丁"}],
    }
    known_subjects = build_known_subjects(home_context)

    event_for_father = EventRecord(
        id=11,
        source_id=1,
        session_id=1,
        event_start_time=datetime(2026, 3, 13, 8, 0, 0),
        description="成员在客厅出现",
        event_type="member_appear",
        summary="爸爸出现在客厅",
        title="成员出现",
        related_entities_json=[
            {
                "entity_type": "member",
                "display_name": "爸爸",
                "matched_profile_name": "爸爸",
                "recognition_status": "confirmed",
            }
        ],
    )

    event_unknown = EventRecord(
        id=12,
        source_id=1,
        session_id=1,
        event_start_time=datetime(2026, 3, 13, 9, 0, 0),
        description="门口有未知人员短暂停留",
        event_type="unknown_person_appear",
        summary="门口出现未知人员",
        title="未知人员出现",
        related_entities_json=[
            {
                "entity_type": "unknown_person",
                "display_name": "陌生人",
                "recognition_status": "unknown",
            }
        ],
    )

    sections, missing_subjects, mapped_event_ids = build_subject_event_mapping(
        [event_for_father, event_unknown],
        known_subjects,
    )

    assert len(sections) == 1
    assert sections[0].subject_name == "爸爸"
    assert sections[0].related_event_count == 1
    assert "布丁" in missing_subjects
    assert mapped_event_ids == {11}


def test_extract_attention_candidates_by_rule() -> None:
    stranger_event = EventRecord(
        id=21,
        source_id=1,
        session_id=1,
        event_start_time=datetime(2026, 3, 13, 10, 0, 0),
        description="未知人员出现",
        event_type="unknown_person_appear",
        summary="未知人员出现",
    )
    normal_event = EventRecord(
        id=22,
        source_id=1,
        session_id=1,
        event_start_time=datetime(2026, 3, 13, 11, 0, 0),
        description="成员停留",
        event_type="member_stay",
        summary="成员停留",
    )
    unknown_person_entity_event = EventRecord(
        id=23,
        source_id=1,
        session_id=1,
        event_start_time=datetime(2026, 3, 13, 12, 0, 0),
        description="画面边缘有人经过",
        event_type="member_appear",
        summary="画面边缘有人经过",
        related_entities_json=[{"entity_type": "unknown_person", "display_name": "陌生人"}],
    )
    focus_event = EventRecord(
        id=24,
        source_id=1,
        session_id=1,
        event_start_time=datetime(2026, 3, 13, 13, 0, 0),
        description="门口放下包裹",
        event_type="pet_activity",
        summary="门口放下包裹",
        focus_matches_json=["express_delivery"],
    )

    candidates = extract_attention_candidates(
        [stranger_event, normal_event, unknown_person_entity_event, focus_event],
        attention_keys=frozenset({"express_delivery"}),
    )

    candidate_ids = {item.event_id for item in candidates}
    assert candidate_ids == {21, 23, 24}
