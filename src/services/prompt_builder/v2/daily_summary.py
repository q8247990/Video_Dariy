"""v2: 日报 prompt 构建器（Jinja2 模板版）。"""

from datetime import date
from typing import Any

from src.core.i18n.locale_directive import get_language_directive
from src.services.prompt_builder.compression.daily_summary_compressor import (
    MAX_DATA_INPUT_PROMPT_CHARS,
    compress_daily_input,
)
from src.services.prompt_builder.engine import render_template


def _render_home_context(home_context: dict[str, Any]) -> str:
    """渲染日报域家庭上下文（KV 紧凑格式）。"""
    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])
    return render_template(
        "daily_summary/home_context.j2",
        home_profile=home_profile,
        members=members,
        pets=pets,
    )


def _render_data_input(
    subject_sections: list[dict[str, Any]],
    missing_subjects: list[str],
    attention_candidates: list[dict[str, Any]],
) -> str:
    """压缩并渲染数据输入段，超长时截断。"""
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
    if len(text) > MAX_DATA_INPUT_PROMPT_CHARS:
        return text[: MAX_DATA_INPUT_PROMPT_CHARS - 1] + "\u2026"
    return text


def build_daily_summary_prompt(
    input_data: dict[str, Any],
    locale: str | None = None,
) -> tuple[str, str]:
    home_context = input_data["home_context"]
    summary_date = input_data["summary_date"]
    time_range = input_data["time_range"]
    subject_sections = input_data["subject_sections"]
    missing_subjects = input_data["missing_subjects"]
    attention_candidates = input_data["attention_candidates"]

    lang_directive = get_language_directive(locale)
    system_prompt = render_template(
        "daily_summary/system_rules.j2",
        lang_directive=lang_directive,
    )

    user_prompt = "\n\n".join(
        [
            _render_home_context(home_context),
            render_template(
                "daily_summary/task_context.j2",
                summary_date=summary_date,
                time_range_start=time_range["start"],
                time_range_end=time_range["end"],
            ),
            _render_data_input(subject_sections, missing_subjects, attention_candidates),
        ]
    )

    return system_prompt, user_prompt


def build_subject_summary_prompt(
    *,
    home_context: dict[str, Any],
    summary_date: date,
    time_range_start: str,
    time_range_end: str,
    subject_section: dict[str, Any],
    locale: str | None = None,
) -> tuple[str, str]:
    subject_name = str(subject_section.get("subject_name") or "未知对象")
    subject_type = str(subject_section.get("subject_type") or "unknown")
    raw_event_count = int(subject_section.get("raw_event_count") or 0)
    raw_clusters = subject_section.get("clusters")
    clusters: list[dict[str, Any]] = []
    if isinstance(raw_clusters, list):
        clusters = [item for item in raw_clusters if isinstance(item, dict)]

    lang_directive = get_language_directive(locale)
    system_prompt = render_template(
        "daily_summary/system_rules.j2",
        lang_directive=lang_directive,
    )

    user_prompt = "\n\n".join(
        [
            _render_home_context(home_context),
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

    return system_prompt, user_prompt


def build_daily_rollup_prompt(
    *,
    home_context: dict[str, Any],
    summary_date: date,
    subject_results: list[dict[str, Any]],
    attention_candidates: list[dict[str, Any]],
    locale: str | None = None,
) -> tuple[str, str]:
    lang_directive = get_language_directive(locale)
    system_prompt = render_template(
        "daily_summary/system_rules.j2",
        lang_directive=lang_directive,
    )

    user_prompt = "\n\n".join(
        [
            _render_home_context(home_context),
            render_template(
                "daily_summary/rollup_body.j2",
                summary_date=summary_date,
                subject_results=subject_results,
                attention_candidates=attention_candidates,
            ),
        ]
    )

    return system_prompt, user_prompt


# ---------------------------------------------------------------------------
# 串行路径独立段构建（供外部按需组合）
# ---------------------------------------------------------------------------


def build_daily_home_context_prompt(home_context: dict[str, Any]) -> str:
    """构建日报域的家庭上下文 prompt 文本。"""
    return _render_home_context(home_context)


def build_daily_task_prompt(
    *,
    summary_date: date,
    time_range_start: str,
    time_range_end: str,
) -> str:
    """构建日报任务上下文 prompt 文本。"""
    return render_template(
        "daily_summary/task_context.j2",
        summary_date=summary_date,
        time_range_start=time_range_start,
        time_range_end=time_range_end,
    )


def build_daily_data_input_prompt(
    *,
    subject_sections: list[dict[str, Any]],
    missing_subjects: list[str],
    attention_candidates: list[dict[str, Any]],
) -> str:
    """构建日报数据输入 prompt 文本（紧凑版）。"""
    return _render_data_input(subject_sections, missing_subjects, attention_candidates)
