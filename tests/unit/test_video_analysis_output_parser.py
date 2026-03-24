import pytest

from src.services.video_analysis.output_parser import (
    RecognitionOutputFormatError,
    parse_video_recognition_output,
)


def test_parse_output_with_standard_structure() -> None:
    raw = """
    {
      "session_summary": {
        "summary_text": "客厅有人经过",
        "activity_level": "medium",
        "main_subjects": ["人类"],
        "has_important_event": true
      },
      "events": [],
      "analysis_notes": []
    }
    """
    result = parse_video_recognition_output(raw)
    assert result.session_summary.summary_text == "客厅有人经过"


def test_parse_output_with_session_summary_as_root_object() -> None:
    raw = """
    {
      "summary_text": "客厅人员短暂进出",
      "activity_level": "medium",
      "main_subjects": ["人类"],
      "has_important_event": false,
      "events": []
    }
    """
    result = parse_video_recognition_output(raw)
    assert result.session_summary.summary_text == "客厅人员短暂进出"
    assert result.events == []


def test_parse_output_with_wrapped_code_block() -> None:
    raw = """
    ```json
    {
      "session_summary": {
        "summary_text": "外层摘要",
        "activity_level": "high",
        "main_subjects": ["人类", "宠物"],
        "has_important_event": true
      },
      "events": [],
      "analysis_notes": []
    }
    ```
    """
    result = parse_video_recognition_output(raw)
    assert result.session_summary.summary_text == "外层摘要"


def test_parse_output_maps_pet_stay_and_normalizes_note_and_activity() -> None:
    raw = """
    {
      "session_summary": {
        "summary_text": "客厅有猫停留",
        "activity_level": "quiet",
        "main_subjects": ["栗子"],
        "has_important_event": false
      },
      "events": [
        {
          "offset_start_sec": 0,
          "offset_end_sec": 10,
          "event_type": "pet_stay",
          "title": "猫咪停留",
          "summary": "猫咪停在客厅",
          "detail": "猫咪停在客厅中央位置。",
          "related_entities": [],
          "observed_actions": [],
          "interpreted_state": [],
          "confidence": 0.8,
          "importance_level": "low"
        }
      ],
      "analysis_notes": [
        {
          "type": "visibility_low",
          "note": "画面较暗"
        }
      ]
    }
    """

    result = parse_video_recognition_output(raw)

    assert result.session_summary.activity_level == "medium"
    assert result.events[0].event_type == "pet_stay"
    assert result.analysis_notes[0].type == "low_visibility"


def test_parse_output_rejects_truncated_top_level_json_even_if_nested_object_is_valid() -> None:
    raw = """
    {
      "session_summary": {
        "summary_text": "外层摘要",
        "activity_level": "medium",
        "main_subjects": ["妈妈"],
        "has_important_event": true
      },
      "events": [
        {
          "offset_start_sec": 0,
          "offset_end_sec": 10,
          "event_type": "pet_activity",
          "title": "猫咪活动",
          "summary": "猫咪在客厅活动",
          "detail": "猫咪在客厅活动",
          "related_entities": [
            {
              "entity_type": "pet",
              "display_name": "栗子"
    """

    with pytest.raises(RecognitionOutputFormatError, match="complete top-level JSON object"):
        parse_video_recognition_output(raw)
