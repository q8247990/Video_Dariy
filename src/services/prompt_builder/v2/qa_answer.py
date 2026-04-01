"""v2: QA 回答 prompt 构建器（Jinja2 模板版）。"""

from src.application.qa.schemas import CompressedEvidence
from src.services.prompt_builder.engine import render_template


def build_qa_answer_prompt(
    question: str,
    now_iso: str,
    timezone: str,
    home_context_text: str,
    evidence: CompressedEvidence,
) -> tuple[str, str]:
    """构建最终回答 prompt。返回 (system_prompt, user_prompt)。"""
    system_prompt = render_template("qa_answer/system_rules.j2")

    user_prompt = render_template(
        "qa_answer/user.j2",
        now_iso=now_iso,
        timezone=timezone,
        home_context_text=home_context_text,
        query_plan_text=evidence.query_plan_text,
        daily_summary_text=evidence.daily_summary_text,
        session_text=evidence.session_text,
        event_text=evidence.event_text,
        question=question,
    )

    return system_prompt, user_prompt
