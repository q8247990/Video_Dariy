"""v2: QA Agentic Loop 的 system prompt 构建器（Jinja2 模板版）。

内容按"固定在前、易变在后"排列，便于服务端 KV 前缀缓存复用：

1. 静态：角色 / 规则 / 事件类型说明（``qa_agent/system_rules.j2``）
2. 半静态：家庭上下文与已知主体（复用 ``shared/home_context.j2``）
3. 动态：当前时间 / 时区 / 系统数据范围（``qa_agent/context.j2``）

约定：``src.services`` 不反向 import ``src.application``，本函数只消费 primitives / dict。
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
