import pytest

from src.services.daily_summary.output_parser import (
    DailySummaryOutputValidationError,
    parse_daily_summary_output,
)


def test_parse_daily_summary_output_success() -> None:
    raw_text = """
    {
      "overall_summary": "昨天家中整体平稳，爸爸在客厅活动较多。",
      "subject_sections": [
        {
          "subject_name": "爸爸",
          "subject_type": "member",
          "summary": "爸爸在上午和下午分别出现在客厅。",
          "attention_needed": false
        }
      ],
      "attention_items": [
        {
          "title": "门口短暂停留",
          "summary": "门口有一次短暂停留，建议留意。",
          "level": "low"
        }
      ]
    }
    """

    result = parse_daily_summary_output(raw_text)

    assert result.overall_summary.startswith("昨天家中整体平稳")
    assert len(result.subject_sections) == 1
    assert result.subject_sections[0].subject_name == "爸爸"
    assert len(result.attention_items) == 1


def test_parse_daily_summary_output_attention_items_too_many() -> None:
    raw_text = """
    {
      "overall_summary": "整体正常。",
      "subject_sections": [],
      "attention_items": [
        {"title": "1", "summary": "1", "level": "low"},
        {"title": "2", "summary": "2", "level": "low"},
        {"title": "3", "summary": "3", "level": "low"},
        {"title": "4", "summary": "4", "level": "low"}
      ]
    }
    """

    with pytest.raises(DailySummaryOutputValidationError):
        parse_daily_summary_output(raw_text)
