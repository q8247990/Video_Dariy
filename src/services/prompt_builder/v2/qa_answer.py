"""v2: QA 回答 prompt 构建器（Jinja2 模板版）。"""

from src.application.qa.schemas import CompressedEvidence, QueryPlan
from src.services.prompt_builder.engine import render_template

QUESTION_MODE_LABELS = {
    "overview": "整体概览",
    "latest": "最新事件",
    "existence": "事件确认",
    "subject_activity": "主体活动",
    "risk_check": "风险排查",
}


def build_qa_answer_prompt(
    question: str,
    now_iso: str,
    timezone: str,
    home_context_text: str,
    query_plan: QueryPlan,
    evidence: CompressedEvidence,
) -> tuple[str, str]:
    """构建最终回答 prompt。返回 (system_prompt, user_prompt)。"""
    mode_label = QUESTION_MODE_LABELS.get(query_plan.question_mode, query_plan.question_mode)

    time_range_text = ""
    if query_plan.time_range:
        time_range_text = (
            f"{query_plan.time_range.start.isoformat()} ~ "
            f"{query_plan.time_range.end.isoformat()}"
        )

    subjects_text = ""
    if query_plan.subjects:
        subjects_text = "\u3001".join(query_plan.subjects)

    event_types_text = ""
    if query_plan.event_types:
        event_types_text = "\u3001".join(query_plan.event_types)

    importance_levels_text = ""
    if query_plan.importance_levels:
        importance_levels_text = "\u3001".join(query_plan.importance_levels)

    system_prompt = render_template("qa_answer/system_rules.j2")

    user_prompt = render_template(
        "qa_answer/user.j2",
        now_iso=now_iso,
        timezone=timezone,
        home_context_text=home_context_text,
        mode_label=mode_label,
        time_range_text=time_range_text,
        subjects_text=subjects_text,
        event_types_text=event_types_text,
        importance_levels_text=importance_levels_text,
        daily_summary_text=evidence.daily_summary_text,
        session_text=evidence.session_text,
        event_text=evidence.event_text,
        question=question,
    )

    return system_prompt, user_prompt
