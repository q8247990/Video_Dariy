"""v2: QA 意图识别 prompt 构建器（Jinja2 模板版）。"""

from datetime import datetime
from typing import Any

from src.services.prompt_builder.engine import render_template


def _extract_subject_names(home_context: dict[str, Any]) -> list[str]:
    """从家庭上下文中提取已知主体名称列表。"""
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])
    subject_names: list[str] = []
    for item in members:
        name = item.get("name", "")
        if name:
            subject_names.append(name)
    for item in pets:
        name = item.get("name", "")
        if name:
            subject_names.append(name)
    return subject_names


def build_qa_intent_prompt(
    question: str,
    now: datetime,
    timezone: str,
    home_context: dict[str, Any],
) -> tuple[str, str]:
    """构建意图识别 prompt。返回 (system_prompt, user_prompt)。"""
    home_profile = home_context.get("home_profile", {})
    subject_names = _extract_subject_names(home_context)

    system_prompt = render_template("qa_intent/system_rules.j2")

    user_prompt = render_template(
        "qa_intent/user.j2",
        now_iso=now.isoformat(),
        timezone=timezone,
        home_profile=home_profile,
        subject_names=subject_names,
        question=question,
    )

    return system_prompt, user_prompt
