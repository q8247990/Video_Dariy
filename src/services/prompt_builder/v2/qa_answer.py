"""v2: QA 回答 prompt 构建器（Jinja2 模板版）。"""

from src.application.qa.evidence_compressor import compress_query_plan
from src.application.qa.schemas import CompressedEvidence, QueryPlan
from src.services.prompt_builder.engine import render_template


def build_qa_answer_prompt(
    question: str,
    now_iso: str,
    timezone: str,
    home_context_text: str,
    query_plan: QueryPlan,
    evidence: CompressedEvidence,
) -> tuple[str, str]:
    """构建最终回答 prompt。返回 (system_prompt, user_prompt)。"""
    system_prompt = render_template("qa_answer/system_rules.j2")

    user_prompt = render_template(
        "qa_answer/user.j2",
        now_iso=now_iso,
        timezone=timezone,
        home_context_text=home_context_text,
        query_plan_text=compress_query_plan(query_plan),
        daily_summary_text=evidence.daily_summary_text,
        session_text=evidence.session_text,
        event_text=evidence.event_text,
        question=question,
    )

    return system_prompt, user_prompt
