"""v2: 日报 prompt 构建器（Jinja2 模板版）。"""

import json
from datetime import date
from typing import Any

from src.services.prompt_builder.compression.daily_summary_compressor import (
    MAX_DATA_INPUT_PROMPT_CHARS,
    compress_daily_input,
)
from src.services.prompt_builder.engine import render_template


def _build_home_context_payload(home_context: dict[str, Any]) -> dict[str, Any]:
    """构建日报域的家庭上下文 JSON payload。"""
    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    return {
        "home_profile": {
            "home_name": home_profile.get("home_name"),
            "family_tags": home_profile.get("family_tags", []),
            "focus_points": home_profile.get("focus_points", []),
            "system_style": home_profile.get("system_style"),
            "style_preference_text": home_profile.get("style_preference_text"),
            "assistant_name": home_profile.get("assistant_name"),
            "home_note": home_profile.get("home_note"),
        },
        "known_subjects": {
            "members": [
                {
                    "name": item.get("name"),
                    "role_type": item.get("role_type"),
                    "age_group": item.get("age_group"),
                }
                for item in members
            ],
            "pets": [
                {
                    "name": item.get("name"),
                    "role_type": item.get("role_type"),
                    "breed": item.get("breed"),
                }
                for item in pets
            ],
        },
    }


def _build_task_context_payload(
    *,
    summary_date: date,
    time_range_start: str,
    time_range_end: str,
) -> dict[str, Any]:
    """构建日报任务上下文 JSON payload。"""
    return {
        "summary_date": str(summary_date),
        "summary_scope": "yesterday",
        "time_range": {
            "start": time_range_start,
            "end": time_range_end,
        },
        "task_goal": "生成给家庭用户阅读的结构化日报",
        "output_schema": {
            "overall_summary": "string",
            "subject_sections": [
                {
                    "subject_name": "string",
                    "subject_type": "member|pet",
                    "summary": "string",
                    "attention_needed": True,
                }
            ],
            "attention_items": [
                {
                    "title": "string",
                    "summary": "string",
                    "level": "low|medium|high",
                }
            ],
        },
        "constraints": [
            "subject_sections 仅包含有明确内容的已知对象",
            "attention_items 最多 3 条，可为空",
            "总字数尽量控制在 900 字以内",
            "仅输出 JSON 对象",
        ],
    }


def build_daily_home_context_prompt(home_context: dict[str, Any]) -> str:
    """构建日报域的家庭上下文 prompt 文本。"""
    payload = _build_home_context_payload(home_context)
    return "家庭上下文：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def build_daily_task_prompt(
    *,
    summary_date: date,
    time_range_start: str,
    time_range_end: str,
) -> str:
    """构建日报任务上下文 prompt 文本。"""
    payload = _build_task_context_payload(
        summary_date=summary_date,
        time_range_start=time_range_start,
        time_range_end=time_range_end,
    )
    return "任务上下文：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def build_daily_data_input_prompt(
    *,
    subject_sections: list[dict[str, Any]],
    missing_subjects: list[str],
    attention_candidates: list[dict[str, Any]],
) -> str:
    """构建日报数据输入 prompt 文本（紧凑版）。"""
    compressed = compress_daily_input(
        subject_sections=subject_sections,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates,
    )
    text = render_template(
        "daily_summary/data_input.j2",
        compressed_subject_sections=compressed["subject_sections"],
        missing_subjects=compressed["missing_subjects"],
        compressed_attention_candidates=compressed["attention_candidates"],
    )
    if len(text) <= MAX_DATA_INPUT_PROMPT_CHARS:
        return text
    return text[: MAX_DATA_INPUT_PROMPT_CHARS - 1] + "\u2026"


def build_daily_summary_prompt(input_data: dict[str, Any]) -> str:
    """构建单次日报 prompt。"""
    home_context = input_data["home_context"]
    summary_date = input_data["summary_date"]
    time_range = input_data["time_range"]
    subject_sections = input_data["subject_sections"]
    missing_subjects = input_data["missing_subjects"]
    attention_candidates = input_data["attention_candidates"]

    home_context_payload = _build_home_context_payload(home_context)
    task_context_payload = _build_task_context_payload(
        summary_date=summary_date,
        time_range_start=time_range["start"],
        time_range_end=time_range["end"],
    )

    compressed = compress_daily_input(
        subject_sections=subject_sections,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates,
    )

    data_input_text = render_template(
        "daily_summary/data_input.j2",
        compressed_subject_sections=compressed["subject_sections"],
        missing_subjects=compressed["missing_subjects"],
        compressed_attention_candidates=compressed["attention_candidates"],
    )
    if len(data_input_text) > MAX_DATA_INPUT_PROMPT_CHARS:
        data_input_text = data_input_text[: MAX_DATA_INPUT_PROMPT_CHARS - 1] + "\u2026"

    sections = [
        "系统总结规则：\n" + render_template("daily_summary/system_rules.j2"),
        render_template(
            "daily_summary/home_context.j2",
            home_context_payload=home_context_payload,
        ),
        render_template(
            "daily_summary/task_context.j2",
            task_context_payload=task_context_payload,
        ),
        data_input_text,
    ]
    return "\n\n".join(sections)


def build_subject_summary_prompt(
    *,
    home_context: dict[str, Any],
    summary_date: date,
    time_range_start: str,
    time_range_end: str,
    subject_section: dict[str, Any],
) -> str:
    """构建串行路径的单对象摘要 prompt。"""
    home_context_payload = _build_home_context_payload(home_context)

    subject_name = str(subject_section.get("subject_name") or "未知对象")
    subject_type = str(subject_section.get("subject_type") or "unknown")
    raw_event_count = int(subject_section.get("raw_event_count") or 0)
    raw_clusters = subject_section.get("clusters")
    clusters: list[dict[str, Any]] = []
    if isinstance(raw_clusters, list):
        clusters = [item for item in raw_clusters if isinstance(item, dict)]

    return "\n\n".join(
        [
            "系统总结规则：\n" + render_template("daily_summary/system_rules.j2"),
            render_template(
                "daily_summary/home_context.j2",
                home_context_payload=home_context_payload,
            ),
            render_template(
                "daily_summary/subject_summary_body.j2",
                summary_date=summary_date,
                time_range_start=time_range_start,
                time_range_end=time_range_end,
                subject_name=subject_name,
                subject_type=subject_type,
                raw_event_count=raw_event_count,
                clusters=clusters,
            ),
        ]
    )


def build_daily_rollup_prompt(
    *,
    home_context: dict[str, Any],
    summary_date: date,
    subject_results: list[dict[str, Any]],
    attention_candidates: list[dict[str, Any]],
) -> str:
    """构建串行路径的汇总 prompt。"""
    home_context_payload = _build_home_context_payload(home_context)

    return "\n\n".join(
        [
            "系统总结规则：\n" + render_template("daily_summary/system_rules.j2"),
            render_template(
                "daily_summary/home_context.j2",
                home_context_payload=home_context_payload,
            ),
            render_template(
                "daily_summary/rollup_body.j2",
                summary_date=summary_date,
                subject_results=subject_results,
                attention_candidates=attention_candidates,
            ),
        ]
    )
