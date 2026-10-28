"""QA agentic-loop system prompt builder (Jinja2 templates).

Sections are ordered most-stable-first so the model server can reuse its
KV prefix cache:

1. static: role / rules / event-type reference (``qa_agent/system_rules.j2``)
2. semi-static: home context and known subjects (``shared/home_context.j2``)
3. dynamic: current time / timezone / data range (``qa_agent/context.j2``)

The builder consumes primitives and dicts only; ``src.services`` never
imports ``src.application``.
"""

from typing import Any

from src.core.i18n.locale_directive import get_language_directive, get_tone_directive
from src.services.prompt_builder.engine import render_template
from src.services.video_analysis.enums import EVENT_TYPE_DEFINITIONS


def _subject_names(home_context: dict[str, Any]) -> str:
    """从家庭上下文提取已知主体名称，多个用顿号连接，空则返回 "无"。"""
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])
    names = [item.get("name", "") for item in members if item.get("name")]
    names += [item.get("name", "") for item in pets if item.get("name")]
    return "、".join(names) if names else "无"


def build_qa_agent_prompt(
    *,
    now_iso: str,
    timezone: str,
    home_context: dict[str, Any],
    data_availability: dict[str, Any],
    locale: str | None = None,
) -> str:
    """构建 QA agent 的 system prompt（单一字符串）。"""
    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    home_context_text = render_template(
        "shared/home_context.j2",
        home_profile=home_profile,
        members=members,
        pets=pets,
    )
    system_rules = render_template(
        "qa_agent/system_rules.j2",
        event_type_definitions=EVENT_TYPE_DEFINITIONS,
        lang_directive=get_language_directive(locale),
        tone_directive=get_tone_directive(locale),
    )
    context_text = render_template(
        "qa_agent/context.j2",
        home_context_text=home_context_text,
        subject_names=_subject_names(home_context),
        now_iso=now_iso,
        timezone=timezone,
        earliest_event_date=data_availability.get("earliest_event_date") or "无数据",
        latest_event_date=data_availability.get("latest_event_date") or "无数据",
        total_event_count=data_availability.get("total_event_count", 0),
    )
    return f"{system_rules}\n\n{context_text}"
