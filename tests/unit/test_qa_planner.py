import json
from datetime import datetime, timedelta

from src.application.qa.planner import (
    build_retrieval_plan,
    normalize_query_plan,
    parse_intent_output,
)
from src.application.qa.schemas import QueryPlan, TimeRange

# ---------------------------------------------------------------------------
# parse_intent_output
# ---------------------------------------------------------------------------


def test_parse_intent_output_valid_json() -> None:
    raw = json.dumps({"question_mode": "overview", "limit": 20})
    result = parse_intent_output(raw)
    assert result is not None
    assert result["question_mode"] == "overview"


def test_parse_intent_output_with_code_block() -> None:
    raw = '```json\n{"question_mode": "latest"}\n```'
    result = parse_intent_output(raw)
    assert result is not None
    assert result["question_mode"] == "latest"


def test_parse_intent_output_with_extra_text() -> None:
    raw = 'Here is the plan: {"question_mode": "risk_check"} done.'
    result = parse_intent_output(raw)
    assert result is not None
    assert result["question_mode"] == "risk_check"


def test_parse_intent_output_invalid_returns_none() -> None:
    assert parse_intent_output("not json at all") is None
    assert parse_intent_output("") is None


# ---------------------------------------------------------------------------
# normalize_query_plan
# ---------------------------------------------------------------------------


def test_normalize_query_plan_valid_input() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    raw = {
        "question_mode": "subject_activity",
        "time_range": {
            "start": "2026-03-21T00:00:00",
            "end": "2026-03-21T12:00:00",
            "time_label": "today",
        },
        "subjects": ["爸爸"],
        "event_types": ["member_appear", "member_stay"],
        "importance_levels": ["high", "medium"],
        "use_daily_summary_first": False,
        "use_session_summary_first": True,
        "need_event_details": True,
        "limit": 25,
    }
    plan = normalize_query_plan(raw, now)

    assert plan.question_mode == "subject_activity"
    assert plan.time_range is not None
    assert plan.time_range.start == datetime(2026, 3, 21, 0, 0, 0)
    assert plan.time_range.end == datetime(2026, 3, 21, 12, 0, 0)
    assert plan.subjects == ["爸爸"]
    assert set(plan.event_types) == {"member_appear", "member_stay"}
    assert set(plan.importance_levels) == {"high", "medium"}
    assert plan.use_daily_summary_first is False
    assert plan.limit == 25


def test_normalize_query_plan_none_returns_default() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    plan = normalize_query_plan(None, now)

    assert plan.question_mode == "overview"
    assert plan.time_range is not None
    assert plan.time_range.end == now
    assert plan.time_range.start == now - timedelta(hours=24)
    assert plan.limit == 30


def test_normalize_query_plan_invalid_mode_falls_back() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    raw = {"question_mode": "invalid_mode"}
    plan = normalize_query_plan(raw, now)
    assert plan.question_mode == "overview"


def test_normalize_query_plan_invalid_event_types_filtered() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    raw = {"event_types": ["member_appear", "fake_type", "pet_activity"]}
    plan = normalize_query_plan(raw, now)
    assert set(plan.event_types) == {"member_appear", "pet_activity"}


def test_normalize_query_plan_swapped_time_range() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    raw = {
        "time_range": {
            "start": "2026-03-21T12:00:00",
            "end": "2026-03-21T00:00:00",
        }
    }
    plan = normalize_query_plan(raw, now)
    assert plan.time_range is not None
    assert plan.time_range.start < plan.time_range.end


def test_normalize_query_plan_limit_clamped() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    plan = normalize_query_plan({"limit": 999}, now)
    assert plan.limit == 50

    plan = normalize_query_plan({"limit": -5}, now)
    assert plan.limit == 1


# ---------------------------------------------------------------------------
# build_retrieval_plan
# ---------------------------------------------------------------------------


def test_build_retrieval_plan_overview_long_range() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    query_plan = QueryPlan(
        question_mode="overview",
        time_range=TimeRange(
            start=now - timedelta(days=3),
            end=now,
        ),
        use_daily_summary_first=True,
        use_session_summary_first=True,
        need_event_details=True,
    )
    rp = build_retrieval_plan(query_plan)

    assert rp.load_daily_summaries is True
    assert rp.load_sessions is True
    assert rp.load_events is True
    assert rp.daily_summary_date_range is not None


def test_build_retrieval_plan_latest_mode() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    query_plan = QueryPlan(
        question_mode="latest",
        time_range=TimeRange(start=now - timedelta(hours=1), end=now),
        use_daily_summary_first=False,
        use_session_summary_first=False,
        need_event_details=True,
    )
    rp = build_retrieval_plan(query_plan)

    assert rp.load_daily_summaries is False
    assert rp.budgets.max_events == 5


def test_build_retrieval_plan_risk_check_budgets() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    query_plan = QueryPlan(
        question_mode="risk_check",
        time_range=TimeRange(start=now - timedelta(hours=12), end=now),
        use_daily_summary_first=False,
        use_session_summary_first=True,
        need_event_details=True,
    )
    rp = build_retrieval_plan(query_plan)

    assert rp.load_sessions is True
    assert rp.budgets.max_sessions == 10
    assert rp.budgets.max_events == 30


def test_build_retrieval_plan_short_range_no_daily() -> None:
    now = datetime(2026, 3, 21, 12, 0, 0)
    query_plan = QueryPlan(
        question_mode="overview",
        time_range=TimeRange(start=now - timedelta(hours=6), end=now),
        use_daily_summary_first=True,
        use_session_summary_first=True,
        need_event_details=True,
    )
    rp = build_retrieval_plan(query_plan)

    # 时间跨度 < 24h，不查日报
    assert rp.load_daily_summaries is False
