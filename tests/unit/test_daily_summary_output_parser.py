import pytest

from src.services.daily_summary.output_parser import (
    DailySummaryOutputFormatError,
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


def test_parse_daily_summary_output_wrapped_code_block() -> None:
    raw_text = """
    ```json
    {
      "overall_summary": "昨日家中整体平稳，客厅活动较多。",
      "subject_sections": [
        {
          "subject_name": "妈妈",
          "subject_type": "member",
          "summary": "妈妈上午在厨房准备早餐。",
          "attention_needed": false
        }
      ],
      "attention_items": [
        {
          "title": "门口停留",
          "summary": "门口有一次短暂停留。",
          "level": "low"
        }
      ]
    }
    ```
    """

    result = parse_daily_summary_output(raw_text)

    assert result.overall_summary.startswith("昨日家中整体平稳")
    assert len(result.subject_sections) == 1
    assert result.subject_sections[0].subject_name == "妈妈"
    assert result.attention_items[0].level == "low"


def test_parse_daily_summary_output_rejects_truncated_json() -> None:
    """非严格 JSON 提取会从首个 ``{`` 取到最后一个 ``}``，外层未闭合时报格式错误。"""

    raw_text = """
    {
      "overall_summary": "整体平稳。",
      "subject_sections": [
        {
          "subject_name": "爸爸",
          "subject_type": "member",
          "summary": "爸爸上午在客厅。",
          "attention_needed": false
        }
      ],
      "attention_items": [
    """

    with pytest.raises(DailySummaryOutputFormatError):
        parse_daily_summary_output(raw_text)


def test_parse_daily_summary_output_rejects_missing_overall_summary() -> None:
    raw_text = """
    {
      "subject_sections": [],
      "attention_items": []
    }
    """

    with pytest.raises(DailySummaryOutputValidationError):
        parse_daily_summary_output(raw_text)


def test_parse_daily_summary_output_rejects_empty_attention_level() -> None:
    raw_text = """
    {
      "overall_summary": "整体正常。",
      "subject_sections": [],
      "attention_items": [
        {"title": "异常", "summary": "出现异常", "level": ""}
      ]
    }
    """

    with pytest.raises(DailySummaryOutputValidationError):
        parse_daily_summary_output(raw_text)


def test_parse_daily_summary_output_rejects_invalid_subject_type() -> None:
    raw_text = """
    {
      "overall_summary": "整体正常。",
      "subject_sections": [
        {
          "subject_name": "幽灵",
          "subject_type": "ghost",
          "summary": "出现不明生物。",
          "attention_needed": false
        }
      ],
      "attention_items": []
    }
    """

    with pytest.raises(DailySummaryOutputValidationError):
        parse_daily_summary_output(raw_text)
