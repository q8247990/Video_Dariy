from src.services.video_analysis.output_parser import parse_video_recognition_output


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


def test_parse_output_prefers_valid_outer_object_when_nested_summary_exists() -> None:
    raw = """
    噪声前缀
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
    噪声后缀
    """
    result = parse_video_recognition_output(raw)
    assert result.session_summary.summary_text == "外层摘要"
