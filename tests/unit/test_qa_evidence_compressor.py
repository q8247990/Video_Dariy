from datetime import date, datetime

from src.application.qa.evidence_compressor import (
    compress_daily_summaries,
    compress_events,
    compress_evidence,
    compress_home_context,
    compress_query_plan,
    compress_sessions,
)
from src.application.qa.schemas import (
    DailySummaryEvidence,
    EventEvidence,
    QueryPlan,
    SessionEvidence,
    TimeRange,
)

# ---------------------------------------------------------------------------
# compress_home_context
# ---------------------------------------------------------------------------


def test_compress_home_context_basic() -> None:
    ctx = {
        "home_profile": {
            "home_name": "温馨之家",
            "family_tags": ["有老人", "有宠物"],
            "focus_points": ["安全", "宠物"],
        },
        "members": [{"name": "爸爸", "role_type": "father"}],
        "pets": [{"name": "布丁", "role_type": "cat"}],
    }
    text = compress_home_context(ctx)
    assert "温馨之家" in text
    assert "爸爸" in text
    assert "布丁" in text


def test_compress_home_context_empty() -> None:
    text = compress_home_context({})
    assert "成员" in text
    assert "宠物" in text


# ---------------------------------------------------------------------------
# compress_query_plan
# ---------------------------------------------------------------------------


def test_compress_query_plan_full() -> None:
    plan = QueryPlan(
        question_mode="subject_activity",
        time_range=TimeRange(
            start=datetime(2026, 3, 20, 0, 0),
            end=datetime(2026, 3, 21, 12, 0),
        ),
        subjects=["爸爸"],
        event_types=["member_appear"],
        importance_levels=["high"],
    )
    text = compress_query_plan(plan)
    assert "subject_activity" in text
    assert "爸爸" in text
    assert "member_appear" in text
    assert "high" in text


def test_compress_query_plan_minimal() -> None:
    plan = QueryPlan(question_mode="overview")
    text = compress_query_plan(plan)
    assert "overview" in text


# ---------------------------------------------------------------------------
# compress_daily_summaries
# ---------------------------------------------------------------------------


def test_compress_daily_summaries() -> None:
    summaries = [
        DailySummaryEvidence(
            summary_date=date(2026, 3, 20),
            overall_summary="昨天家中整体平稳，爸爸在客厅活动。",
            subject_sections=[{"subject_name": "爸爸", "summary": "爸爸上午在客厅活动。"}],
            attention_items=[
                {"title": "门口未知人员", "summary": "门口有短暂停留", "level": "medium"}
            ],
            event_count=12,
        )
    ]
    text = compress_daily_summaries(summaries)
    assert "2026-03-20" in text
    assert "event_count=12" in text
    assert "门口未知人员" in text
    assert "爸爸" in text


def test_compress_daily_summaries_empty() -> None:
    assert compress_daily_summaries([]) == ""


# ---------------------------------------------------------------------------
# compress_sessions
# ---------------------------------------------------------------------------


def test_compress_sessions() -> None:
    sessions = [
        SessionEvidence(
            id=45,
            session_start_time=datetime(2026, 3, 20, 9, 0),
            session_end_time=datetime(2026, 3, 20, 9, 12),
            summary_text="爸爸在客厅持续活动",
            activity_level="medium",
            main_subjects=["爸爸"],
            has_important_event=True,
        )
    ]
    text = compress_sessions(sessions)
    assert "S45" in text
    assert "medium" in text
    assert "爸爸" in text
    assert "important=yes" in text


def test_compress_sessions_empty() -> None:
    assert compress_sessions([]) == ""


# ---------------------------------------------------------------------------
# compress_events
# ---------------------------------------------------------------------------


def test_compress_events() -> None:
    events = [
        EventEvidence(
            id=123,
            session_id=45,
            event_start_time=datetime(2026, 3, 20, 9, 3),
            event_type="member_appear",
            importance_level="medium",
            title="成员出现",
            summary="爸爸上午出现在客厅并活动",
            detail="爸爸出现在客厅并持续停留，期间有明显移动",
            related_entities=[
                {
                    "matched_profile_name": "爸爸",
                    "display_name": "爸爸",
                    "recognition_status": "confirmed",
                }
            ],
        )
    ]
    text = compress_events(events)
    assert "E123" in text
    assert "member_appear" in text
    assert "爸爸" in text
    assert "detail=" in text


def test_compress_events_filters_unrecognized_entities() -> None:
    events = [
        EventEvidence(
            id=200,
            session_id=50,
            event_start_time=datetime(2026, 3, 20, 10, 0),
            event_type="unknown_person_appear",
            importance_level="high",
            title="未知人员",
            summary="门口出现未知人员",
            detail="门口出现未知人员短暂停留",
            related_entities=[
                {
                    "display_name": "陌生人",
                    "recognition_status": "unknown",
                }
            ],
        )
    ]
    text = compress_events(events)
    assert "subject=无" in text


def test_compress_events_empty() -> None:
    assert compress_events([]) == ""


# ---------------------------------------------------------------------------
# compress_evidence (总入口)
# ---------------------------------------------------------------------------


def test_compress_evidence_integration() -> None:
    home_context = {
        "home_profile": {"home_name": "测试家庭", "family_tags": [], "focus_points": []},
        "members": [{"name": "爸爸", "role_type": "father"}],
        "pets": [],
    }
    query_plan = QueryPlan(
        question_mode="overview",
        time_range=TimeRange(
            start=datetime(2026, 3, 20, 0, 0),
            end=datetime(2026, 3, 21, 0, 0),
        ),
    )
    evidence = compress_evidence(
        home_context=home_context,
        query_plan=query_plan,
        daily_summaries=[],
        sessions=[],
        events=[
            EventEvidence(
                id=1,
                session_id=1,
                event_start_time=datetime(2026, 3, 20, 9, 0),
                event_type="member_appear",
                importance_level="medium",
                title="成员出现",
                summary="爸爸出现",
                detail="爸爸出现在客厅",
            )
        ],
    )

    assert "测试家庭" in evidence.home_context_text
    assert "overview" in evidence.query_plan_text
    assert evidence.daily_summary_text == ""
    assert evidence.session_text == ""
    assert "E1" in evidence.event_text
