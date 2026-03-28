"""v2: QA 意图识别 prompt 构建器（Jinja2 模板版）。"""

from datetime import datetime
from typing import Any

from src.services.prompt_builder.engine import render_template

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
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])
    subject_names = _extract_subject_names(home_context)

    system_prompt = render_template("qa_intent/system_rules.j2")

    user_prompt = render_template(
        "qa_intent/user.j2",
        now_iso=now.isoformat(),
        timezone=timezone,
        home_profile=home_profile,
        members=members,
        pets=pets,
        subject_names=subject_names,
        event_type_definitions=EVENT_TYPE_DEFINITIONS,
        question_mode_definitions=QUESTION_MODE_DEFINITIONS,
        query_plan_schema=QUERY_PLAN_SCHEMA,
        question=question,
    )

    return system_prompt, user_prompt
