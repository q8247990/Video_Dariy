from src.application.qa.schemas import CompressedEvidence, QueryPlan
from src.services.prompt_builder.templates.qa_answer.system_rules import (
    SYSTEM_QA_ANSWER_RULES_PROMPT,
)

QUESTION_MODE_LABELS = {
    "overview": "整体概览",
    "latest": "最新事件",
    "existence": "事件确认",
    "subject_activity": "主体活动",
    "risk_check": "风险排查",
}


def _build_query_plan_section(query_plan: QueryPlan) -> str:
    mode_label = QUESTION_MODE_LABELS.get(query_plan.question_mode, query_plan.question_mode)
    lines = [
        "查询计划：",
        f"问答模式: {mode_label}",
    ]
    if query_plan.time_range:
        lines.append(
            f"时间范围: {query_plan.time_range.start.isoformat()} ~ "
            f"{query_plan.time_range.end.isoformat()}"
        )
    if query_plan.subjects:
        lines.append(f"关注主体: {'、'.join(query_plan.subjects)}")
    if query_plan.event_types:
        lines.append(f"事件类型: {'、'.join(query_plan.event_types)}")
    if query_plan.importance_levels:
        lines.append(f"重要性: {'、'.join(query_plan.importance_levels)}")
    return "\n".join(lines)


def build_qa_answer_prompt(
    question: str,
    now_iso: str,
    timezone: str,
    home_context_text: str,
    query_plan: QueryPlan,
    evidence: CompressedEvidence,
) -> tuple[str, str]:
    """构建最终回答 prompt。

    Returns:
        (system_prompt, user_prompt)
    """
    query_plan_section = _build_query_plan_section(query_plan)

    sections: list[str] = [
        f"当前时间: {now_iso}",
        f"时区: {timezone}",
        home_context_text,
        query_plan_section,
    ]

    if evidence.daily_summary_text:
        sections.append(f"日报证据：\n{evidence.daily_summary_text}")

    if evidence.session_text:
        sections.append(f"会话证据：\n{evidence.session_text}")

    if evidence.event_text:
        sections.append(f"事件证据：\n{evidence.event_text}")

    if not evidence.daily_summary_text and not evidence.session_text and not evidence.event_text:
        sections.append("证据：当前时间范围内未检索到相关记录。")

    sections.append(f"用户问题: {question}")

    user_prompt = "\n\n".join(sections)
    return SYSTEM_QA_ANSWER_RULES_PROMPT, user_prompt
