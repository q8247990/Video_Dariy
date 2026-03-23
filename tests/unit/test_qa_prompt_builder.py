from datetime import datetime

from src.application.qa.schemas import CompressedEvidence, QueryPlan, TimeRange
from src.services.prompt_builder.qa_answer import build_qa_answer_prompt
from src.services.prompt_builder.qa_intent import build_qa_intent_prompt

# ---------------------------------------------------------------------------
# qa_intent prompt builder
# ---------------------------------------------------------------------------


def test_build_qa_intent_prompt_basic() -> None:
    home_context = {
        "home_profile": {
            "home_name": "温馨之家",
            "family_tags": ["有老人"],
            "focus_points": ["安全"],
        },
        "members": [{"name": "爸爸", "role_type": "father"}],
        "pets": [{"name": "布丁", "role_type": "cat"}],
    }
    system_prompt, user_prompt = build_qa_intent_prompt(
        question="今天家里发生了什么？",
        now=datetime(2026, 3, 21, 12, 0, 0),
        timezone="Asia/Shanghai",
        home_context=home_context,
    )

    assert "意图理解" in system_prompt
    assert "今天家里发生了什么" in user_prompt
    assert "爸爸" in user_prompt
    assert "布丁" in user_prompt
    assert "Asia/Shanghai" in user_prompt
    assert "member_appear" in user_prompt
    assert "overview" in user_prompt


def test_build_qa_intent_prompt_empty_home() -> None:
    home_context = {
        "home_profile": {"home_name": "", "family_tags": [], "focus_points": []},
        "members": [],
        "pets": [],
    }
    system_prompt, user_prompt = build_qa_intent_prompt(
        question="最近有异常吗？",
        now=datetime(2026, 3, 21, 12, 0, 0),
        timezone="Asia/Shanghai",
        home_context=home_context,
    )

    assert "已知主体列表: 无" in user_prompt
    assert "最近有异常吗" in user_prompt


# ---------------------------------------------------------------------------
# qa_answer prompt builder
# ---------------------------------------------------------------------------


def test_build_qa_answer_prompt_with_evidence() -> None:
    query_plan = QueryPlan(
        question_mode="overview",
        time_range=TimeRange(
            start=datetime(2026, 3, 20, 0, 0),
            end=datetime(2026, 3, 21, 0, 0),
        ),
        subjects=["爸爸"],
    )
    evidence = CompressedEvidence(
        home_context_text="家庭: 温馨之家\n成员: 爸爸",
        query_plan_text="模式=overview | 主体=爸爸",
        daily_summary_text="D 2026-03-20 | event_count=5 | overall=整体平稳",
        session_text="S1 | 03-20 09:00~09:12 | activity=medium",
        event_text="E1 | 03-20 09:03 | medium | member_appear | subject=爸爸 | summary=爸爸出现",
    )

    system_prompt, user_prompt = build_qa_answer_prompt(
        question="昨天爸爸做了什么？",
        now_iso="2026-03-21T12:00:00",
        timezone="Asia/Shanghai",
        home_context_text=evidence.home_context_text,
        query_plan=query_plan,
        evidence=evidence,
    )

    assert "证据" in system_prompt
    assert "昨天爸爸做了什么" in user_prompt
    assert "日报证据" in user_prompt
    assert "会话证据" in user_prompt
    assert "事件证据" in user_prompt
    assert "爸爸" in user_prompt


def test_build_qa_answer_prompt_no_evidence() -> None:
    query_plan = QueryPlan(
        question_mode="existence",
        time_range=TimeRange(
            start=datetime(2026, 3, 21, 0, 0),
            end=datetime(2026, 3, 21, 12, 0),
        ),
    )
    evidence = CompressedEvidence(
        home_context_text="家庭: 测试",
    )

    system_prompt, user_prompt = build_qa_answer_prompt(
        question="今天有没有陌生人？",
        now_iso="2026-03-21T12:00:00",
        timezone="Asia/Shanghai",
        home_context_text=evidence.home_context_text,
        query_plan=query_plan,
        evidence=evidence,
    )

    assert "未检索到相关记录" in user_prompt
