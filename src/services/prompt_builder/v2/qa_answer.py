"""v2: QA 回答 prompt 构建器（Jinja2 模板版）。

The ``build_qa_answer_prompt`` helper is intentionally a pure function
over primitive strings — it lives in ``src.services`` and must not
import from ``src.application``. The application-layer
``QAService._answer_via_legacy`` path passes the four ``CompressedEvidence``
text fields directly; this keeps the prompt builder free of any DTO
types so it can be tested in isolation and reused by future call sites.
"""

from src.core.i18n.locale_directive import get_language_directive
from src.services.prompt_builder.engine import render_template


def build_qa_answer_prompt(
    question: str,
    now_iso: str,
    timezone: str,
    home_context_text: str,
    *,
    query_plan_text: str,
    daily_summary_text: str,
    session_text: str,
    event_text: str,
    locale: str | None = None,
) -> tuple[str, str]:
    lang_directive = get_language_directive(locale)

    system_prompt = render_template(
        "qa_answer/system_rules.j2",
        lang_directive=lang_directive,
    )

    user_prompt = render_template(
        "qa_answer/user.j2",
        now_iso=now_iso,
        timezone=timezone,
        home_context_text=home_context_text,
        query_plan_text=query_plan_text,
        daily_summary_text=daily_summary_text,
        session_text=session_text,
        event_text=event_text,
        question=question,
    )

    return system_prompt, user_prompt
