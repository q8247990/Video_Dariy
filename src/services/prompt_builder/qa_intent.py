import json
from datetime import datetime
from typing import Any

from src.services.prompt_builder.templates.qa_intent.system_rules import (
    SYSTEM_QA_INTENT_RULES_PROMPT,
)

EVENT_TYPE_DEFINITIONS: list[dict[str, str]] = [
    {"type": "member_appear", "desc": "家庭成员出现在画面中"},
    {"type": "member_enter", "desc": "成员进入画面或区域"},
    {"type": "member_leave", "desc": "成员离开画面或区域"},
    {"type": "member_stay", "desc": "成员持续停留"},
    {"type": "unknown_person_appear", "desc": "陌生人/未知人员出现"},
    {"type": "pet_appear", "desc": "宠物出现在画面中"},
    {"type": "pet_activity", "desc": "宠物活动（走动、玩耍等）"},
    {"type": "pet_rest", "desc": "宠物休息或静止"},
    {"type": "member_pet_interaction", "desc": "成员与宠物互动"},
    {"type": "multi_entity_interaction", "desc": "多个对象之间互动"},
    {"type": "abnormal_stay", "desc": "异常停留（长时间不动、异常位置等）"},
    {"type": "scene_attention_needed", "desc": "场景需要关注（异常光线、物品移动等）"},
]

QUESTION_MODE_DEFINITIONS: list[dict[str, str]] = [
    {"mode": "overview", "desc": "了解整体情况、概览、总结"},
    {"mode": "latest", "desc": "查看最新/最近一条事件"},
    {"mode": "existence", "desc": "确认某件事是否发生过"},
    {"mode": "subject_activity", "desc": "了解某个具体主体（人/宠物）的活动"},
    {"mode": "risk_check", "desc": "了解是否有风险、异常、需要关注的事"},
]

QUERY_PLAN_SCHEMA = {
    "question_mode": "overview|latest|existence|subject_activity|risk_check",
    "time_range": {
        "start": "ISO8601 datetime with timezone",
        "end": "ISO8601 datetime with timezone",
        "time_label": "string, e.g. today/yesterday/last_3_hours",
    },
    "subjects": ["string, from known subject list"],
    "event_types": ["string, from event type enum"],
    "importance_levels": ["low|medium|high"],
    "use_daily_summary_first": True,
    "use_session_summary_first": True,
    "need_event_details": True,
    "limit": 30,
}


def _build_home_context_section(home_context: dict[str, Any]) -> tuple[str, list[str]]:
    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    subject_names: list[str] = []
    member_lines: list[str] = []
    for item in members:
        name = item.get("name", "")
        role = item.get("role_type", "")
        member_lines.append(f"- {name}（{role}）")
        if name:
            subject_names.append(name)

    pet_lines: list[str] = []
    for item in pets:
        name = item.get("name", "")
        role = item.get("role_type", "")
        pet_lines.append(f"- {name}（{role}）")
        if name:
            subject_names.append(name)

    lines = [
        "家庭上下文：",
        f"家庭名称: {home_profile.get('home_name', '')}",
        f"关注重点: {'、'.join(home_profile.get('focus_points', [])) or '无'}",
        "家庭成员:",
        *(member_lines if member_lines else ["- 无"]),
        "宠物:",
        *(pet_lines if pet_lines else ["- 无"]),
    ]
    return "\n".join(lines), subject_names


def _build_event_type_section() -> str:
    lines = ["事件类型说明："]
    for item in EVENT_TYPE_DEFINITIONS:
        lines.append(f"- {item['type']}: {item['desc']}")
    return "\n".join(lines)


def _build_question_mode_section() -> str:
    lines = ["问答模式说明："]
    for item in QUESTION_MODE_DEFINITIONS:
        lines.append(f"- {item['mode']}: {item['desc']}")
    return "\n".join(lines)


def build_qa_intent_prompt(
    question: str,
    now: datetime,
    timezone: str,
    home_context: dict[str, Any],
) -> tuple[str, str]:
    """构建意图识别 prompt。

    Returns:
        (system_prompt, user_prompt)
    """
    home_section, subject_names = _build_home_context_section(home_context)
    event_type_section = _build_event_type_section()
    question_mode_section = _build_question_mode_section()

    schema_text = json.dumps(QUERY_PLAN_SCHEMA, ensure_ascii=False, indent=2)

    user_prompt = "\n\n".join(
        [
            f"当前时间: {now.isoformat()}",
            f"时区: {timezone}",
            home_section,
            f"已知主体列表: {', '.join(subject_names) if subject_names else '无'}",
            event_type_section,
            question_mode_section,
            f"输出 schema（严格遵守）:\n{schema_text}",
            f"用户问题: {question}",
        ]
    )

    return SYSTEM_QA_INTENT_RULES_PROMPT, user_prompt
