import pytest

from src.services.video_analysis.output_parser import (
    RecognitionOutputValidationError,
    parse_video_recognition_output,
)


def test_parse_video_recognition_output_success() -> None:
    raw_text = """
    {
      "session_summary": {
        "summary_text": "一名成员经过客厅并短暂停留",
        "activity_level": "medium",
        "main_subjects": ["爸爸"],
        "has_important_event": true
      },
      "events": [
        {
          "offset_start_sec": 1,
          "offset_end_sec": 5,
          "event_type": "member_appear",
          "title": "成员出现",
          "summary": "一名成员进入客厅",
          "detail": "该成员从右侧进入客厅，在沙发前停留约4秒后离开画面中心。",
          "related_entities": [
            {
              "entity_type": "member",
              "display_name": "爸爸",
              "matched_profile_name": "爸爸",
              "recognition_status": "confirmed",
              "confidence": 0.91
            }
          ],
          "observed_actions": ["走入画面"],
          "interpreted_state": ["活跃"],
          "confidence": 0.9,
          "importance_level": "medium"
        }
      ],
      "analysis_notes": []
    }
    """

    result = parse_video_recognition_output(raw_text)

    assert result.session_summary.activity_level == "medium"
    assert len(result.events) == 1
    assert result.events[0].event_type == "member_appear"


def test_parse_video_recognition_output_invalid_event_type_falls_back_to_other() -> None:
    raw_text = """
    {
      "session_summary": {
        "summary_text": "无",
        "activity_level": "low",
        "main_subjects": [],
        "has_important_event": false
      },
      "events": [
        {
          "offset_start_sec": 0,
          "offset_end_sec": 1,
          "event_type": "invalid_type",
          "title": "test",
          "summary": "test",
          "detail": "test detail",
          "related_entities": [],
          "observed_actions": [],
          "interpreted_state": [],
          "confidence": 0.8,
          "importance_level": "low"
        }
      ],
      "analysis_notes": []
    }
    """

    result = parse_video_recognition_output(raw_text)

    assert result.events[0].event_type == "other"


def test_parse_video_recognition_output_invalid_importance_level_still_fails() -> None:
    raw_text = """
    {
      "session_summary": {
        "summary_text": "无",
        "activity_level": "low",
        "main_subjects": [],
        "has_important_event": false
      },
      "events": [
        {
          "offset_start_sec": 0,
          "offset_end_sec": 1,
          "event_type": "pet_activity",
          "title": "test",
          "summary": "test",
          "detail": "test detail",
          "related_entities": [],
          "observed_actions": [],
          "interpreted_state": [],
          "confidence": 0.8,
          "importance_level": "urgent"
        }
      ],
      "analysis_notes": []
    }
    """

    with pytest.raises(RecognitionOutputValidationError, match="importance_level"):
        parse_video_recognition_output(raw_text)
