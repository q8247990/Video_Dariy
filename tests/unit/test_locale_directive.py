from typing import Any

from src.core.i18n.locale_directive import (
    get_fallback_summary,
    get_language_directive,
    get_mcp_field_description,
    get_mcp_tool_description,
    get_summary_title,
    get_tone_directive,
)
from src.services.prompt_builder.v2.daily_summary import build_daily_summary_prompt


def test_language_directive_switches_and_defaults_to_zh() -> None:
    assert get_language_directive("en-US") == "Please respond in English."
    assert get_language_directive("zh-CN") == "请使用简体中文回答。"
    assert get_language_directive(None) == "请使用简体中文回答。"


def test_tone_and_fallback_switch_with_locale() -> None:
    assert get_tone_directive("en-US") != get_tone_directive("zh-CN")
    assert "calm" in get_fallback_summary(False, "en-US").lower()
    assert "平稳" in get_fallback_summary(False, "zh-CN")


def test_summary_title_and_mcp_descriptions_switch_with_locale() -> None:
    assert get_summary_title("2026-03-20", "en-US") == "2026-03-20 Daily Summary"
    assert get_summary_title("2026-03-20", "zh-CN") == "2026-03-20 家庭日报"
    assert get_mcp_tool_description("search_events", "en-US").startswith("Search events")
    assert get_mcp_field_description("limit", "en-US").startswith("Maximum")


def _daily_input() -> dict[str, Any]:
    return {
        "home_context": {"home_profile": {}, "members": [], "pets": []},
        "summary_date": "2026-03-20",
        "time_range": {"start": "2026-03-20T00:00:00", "end": "2026-03-20T23:59:59"},
        "subject_sections": [],
        "missing_subjects": [],
        "attention_candidates": [],
    }


def test_daily_summary_prompt_injects_locale_directive() -> None:
    system_en, _ = build_daily_summary_prompt(_daily_input(), locale="en-US")
    system_zh, _ = build_daily_summary_prompt(_daily_input(), locale="zh-CN")
    assert "Please respond in English." in system_en
    assert "请使用简体中文回答。" in system_zh
