from datetime import date
from zoneinfo import ZoneInfo

from src.services.home_timezone import format_local_day_range, local_day_bounds
from src.services.prompt_builder.v2.daily_summary import (
    build_daily_data_input_prompt,
    build_daily_summary_prompt,
    build_subject_summary_prompt,
)


def _shanghai_day_range_text() -> str:
    zone = ZoneInfo("Asia/Shanghai")
    start, end = local_day_bounds(zone, date(2026, 11, 22))
    return format_local_day_range(start, end, zone)


def _minimal_home_context() -> dict[str, object]:
    return {"home_profile": {}, "members": [], "pets": []}


def test_daily_summary_prompt_renders_local_range_without_utc_iso() -> None:
    range_text = _shanghai_day_range_text()

    _system, user_prompt = build_daily_summary_prompt(
        {
            "home_context": _minimal_home_context(),
            "summary_date": date(2026, 11, 22),
            "time_range": {"start": range_text, "end": ""},
            "subject_sections": [],
            "missing_subjects": [],
            "attention_candidates": [],
        }
    )

    assert "时间范围=2026-11-22 00:00 ~ 2026-11-23 00:00（Asia/Shanghai）" in user_prompt
    assert "+00:00" not in user_prompt
    assert "T16:00:00" not in user_prompt


def test_subject_summary_prompt_renders_local_range_without_utc_iso() -> None:
    range_text = _shanghai_day_range_text()

    _system, user_prompt = build_subject_summary_prompt(
        home_context=_minimal_home_context(),
        summary_date=date(2026, 11, 22),
        time_range_start=range_text,
        time_range_end="",
        subject_section={
            "subject_name": "布丁",
            "subject_type": "pet",
            "raw_event_count": 0,
            "clusters": [],
        },
    )

    assert "时间范围=2026-11-22 00:00 ~ 2026-11-23 00:00（Asia/Shanghai）" in user_prompt
    assert "+00:00" not in user_prompt
    assert "T16:00:00" not in user_prompt


def test_system_rules_forbid_invented_explanations_for_missing_data() -> None:
    system_prompt, _ = build_daily_summary_prompt(
        {
            "home_context": _minimal_home_context(),
            "summary_date": date(2026, 11, 22),
            "time_range": {"start": _shanghai_day_range_text(), "end": ""},
            "subject_sections": [],
            "missing_subjects": [],
            "attention_candidates": [],
        }
    )

    assert "10. 数据缺失说明：不得为缺失的数据编造原因；数据不足时如实说明。" in system_prompt
    assert "11. 请使用简体中文回答。" in system_prompt


def test_build_daily_data_input_prompt_compacts_repeated_subject_events() -> None:
    repeated_events = [
        {
            "event_id": idx,
            "event_type": "pet_activity",
            "title": "玳瑁猫行走",
            "summary": "一只玳瑁猫进入画面并行走。",
            "importance_level": "medium",
            "recognition_status": "confirmed",
        }
        for idx in range(1, 31)
    ]
    subject_sections = [
        {
            "subject_name": "嘟嘟",
            "subject_type": "pet",
            "related_event_count": len(repeated_events),
            "related_event_summaries": repeated_events,
        }
    ]
    attention_candidates = [
        {
            "event_id": 901,
            "event_type": "scene_attention_needed",
            "title": "植物遮挡",
            "summary": "画面左侧和中间被植物叶片遮挡，影响观察。",
            "importance_level": "medium",
        },
        {
            "event_id": 902,
            "event_type": "scene_attention_needed",
            "title": "植物遮挡",
            "summary": "画面左侧和中间被植物叶片遮挡，影响观察。",
            "importance_level": "medium",
        },
    ]

    prompt = build_daily_data_input_prompt(
        subject_sections=subject_sections,
        missing_subjects=["西瓜"],
        attention_candidates=attention_candidates,
    )

    assert "数据输入（紧凑版）" in prompt
    assert "对象=嘟嘟(pet)" in prompt
    assert "次数=30" in prompt
    assert prompt.count("玳瑁猫行走") == 1
    assert "未命中对象：西瓜" in prompt
    assert "植物遮挡" in prompt
    assert "次数=2" in prompt


def test_build_daily_data_input_prompt_limits_subject_clusters() -> None:
    events = [
        {
            "event_id": idx,
            "event_type": f"pet_activity_{idx}",
            "title": f"事件标题{idx}",
            "summary": f"事件摘要{idx}",
            "importance_level": "medium" if idx % 2 else "low",
            "recognition_status": "confirmed",
        }
        for idx in range(1, 40)
    ]
    prompt = build_daily_data_input_prompt(
        subject_sections=[
            {
                "subject_name": "栗子",
                "subject_type": "pet",
                "related_event_count": len(events),
                "related_event_summaries": events,
            }
        ],
        missing_subjects=[],
        attention_candidates=[],
    )

    cluster_lines = [line for line in prompt.splitlines() if line.strip().startswith("01.")]
    assert cluster_lines

    numbered_cluster_lines = [
        line for line in prompt.splitlines() if line.startswith("  ") and ". [" in line
    ]
    assert len(numbered_cluster_lines) <= 16
