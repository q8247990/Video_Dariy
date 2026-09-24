import logging
from typing import Any

from sqlalchemy import ColumnElement, case, or_, text
from sqlalchemy.orm import Session

from src.application.qa.schemas import (
    DailySummaryEvidence,
    EventEvidence,
    RetrievalPlan,
    SessionEvidence,
)
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.home_profile import HomeProfile
from src.models.video_session import VideoSession
from src.schemas.home_profile import coerce_focus_points
from src.services.attention import ATTENTION_EVENT_TYPES, attention_focus_keys, is_attention_event

logger = logging.getLogger(__name__)


def _attention_focus_keys(db: Session) -> frozenset[str]:
    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    if profile is None:
        return frozenset()
    return attention_focus_keys(coerce_focus_points(profile.focus_points_json))


def _attention_condition(db: Session, keys: frozenset[str]) -> ColumnElement[bool]:
    conditions: list[Any] = [EventRecord.event_type.in_(sorted(ATTENTION_EVENT_TYPES))]
    conditions.append(
        text(
            "EXISTS (SELECT 1 FROM json_array_elements(event_record.related_entities_json) e "
            "WHERE e->>'entity_type' = 'unknown_person')"
        )
    )
    if keys:
        keys_literal = ", ".join(f"'{key}'" for key in sorted(keys))
        conditions.append(
            text(f"(event_record.focus_matches_json::jsonb ?| array[{keys_literal}])")
        )
    return or_(*conditions)


# ---------------------------------------------------------------------------
# 日报层检索
# ---------------------------------------------------------------------------


def retrieve_daily_summaries(
    db: Session,
    plan: RetrievalPlan,
) -> list[DailySummaryEvidence]:
    if not plan.load_daily_summaries or plan.daily_summary_date_range is None:
        return []

    dr = plan.daily_summary_date_range
    rows = (
        db.query(DailySummary)
        .filter(
            DailySummary.summary_date >= dr.start_date,
            DailySummary.summary_date <= dr.end_date,
        )
        .order_by(DailySummary.summary_date.desc())
        .limit(plan.budgets.max_daily_summaries)
        .all()
    )

    results: list[DailySummaryEvidence] = []
    for row in rows:
        results.append(
            DailySummaryEvidence(
                summary_date=row.summary_date,
                overall_summary=row.overall_summary or "",
                subject_sections=row.subject_sections_json or [],
                attention_items=row.attention_items_json or [],
                event_count=row.event_count or 0,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Session 层检索
# ---------------------------------------------------------------------------


def retrieve_sessions(
    db: Session,
    plan: RetrievalPlan,
    question_mode: str,
) -> list[SessionEvidence]:
    if not plan.load_sessions or plan.session_time_range is None:
        return []

    tr = plan.session_time_range
    query = db.query(VideoSession).filter(
        VideoSession.session_start_time >= tr.start,
        VideoSession.session_start_time <= tr.end,
        VideoSession.analysis_status == "success",
    )

    # 排序策略
    if question_mode == "risk_check":
        attention_score = case((VideoSession.has_attention_event.is_(True), 1), else_=0)
        query = query.order_by(attention_score.desc(), VideoSession.session_start_time.desc())
    else:
        query = query.order_by(VideoSession.session_start_time.desc())

    rows = query.limit(plan.budgets.max_sessions).all()

    results: list[SessionEvidence] = []
    for row in rows:
        results.append(
            SessionEvidence(
                id=row.id,
                session_start_time=row.session_start_time,
                session_end_time=row.session_end_time,
                summary_text=row.summary_text or "",
                activity_level=row.activity_level or "",
                main_subjects=row.main_subjects_json or [],
                has_attention_event=bool(row.has_attention_event),
                analysis_notes=row.analysis_notes_json or [],
            )
        )
    return results


# ---------------------------------------------------------------------------
# Event 层检索
# ---------------------------------------------------------------------------


def retrieve_events(
    db: Session,
    plan: RetrievalPlan,
    question_mode: str,
) -> list[EventEvidence]:
    if not plan.load_events or plan.event_time_range is None:
        return []

    tr = plan.event_time_range
    query = (
        db.query(EventRecord)
        .join(VideoSession)
        .filter(
            VideoSession.analysis_status == "success",
            EventRecord.event_start_time >= tr.start,
            EventRecord.event_start_time <= tr.end,
        )
    )

    filters = plan.event_filters

    # event_type 过滤
    if filters.event_types:
        query = query.filter(EventRecord.event_type.in_(filters.event_types))

    attention_keys = _attention_focus_keys(db)

    if filters.attention is True:
        query = query.filter(_attention_condition(db, attention_keys))
    elif filters.attention is False:
        query = query.filter(~_attention_condition(db, attention_keys))

    # 主体过滤：PostgreSQL JSON 查询
    if filters.subjects:
        subject_conditions = _build_subject_filter(filters.subjects)
        if subject_conditions is not None:
            query = query.filter(subject_conditions)

    # 排序策略
    if question_mode == "risk_check":
        attention_score = case((_attention_condition(db, attention_keys), 1), else_=0)
        query = query.order_by(attention_score.desc(), EventRecord.event_start_time.desc())
    elif question_mode == "latest":
        query = query.order_by(EventRecord.event_start_time.desc())
    else:
        query = query.order_by(EventRecord.event_start_time.desc())

    rows = query.limit(plan.budgets.max_events).all()

    results: list[EventEvidence] = []
    for row in rows:
        results.append(
            EventEvidence(
                id=row.id,
                session_id=row.session_id,
                event_start_time=row.event_start_time,
                event_type=row.event_type or "",
                attention=is_attention_event(
                    event_type=row.event_type,
                    related_entities=row.related_entities_json,
                    focus_matches=row.focus_matches_json,
                    attention_keys=attention_keys,
                ),
                title=row.title or "",
                summary=row.summary or "",
                detail=row.detail or "",
                related_entities=row.related_entities_json or [],
                observed_actions=row.observed_actions_json or [],
                interpreted_state=row.interpreted_state_json or [],
            )
        )
    return results


def _build_subject_filter(subjects: list[str]) -> Any:
    """构建 PostgreSQL JSON 主体过滤条件。

    在 related_entities_json 中匹配 matched_profile_name 或 display_name，
    且 recognition_status 为 confirmed 或 suspected。
    """
    from sqlalchemy import or_

    conditions = []
    for name in subjects:
        # 匹配 matched_profile_name
        conditions.append(
            text(
                "EXISTS ("
                "  SELECT 1 FROM jsonb_array_elements(related_entities_json::jsonb) AS elem"
                "  WHERE ("
                "    elem->>'matched_profile_name' = :name"
                "    OR elem->>'display_name' = :name"
                "  )"
                "  AND elem->>'recognition_status' IN ('confirmed', 'suspected')"
                ")"
            ).bindparams(name=name)
        )

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return or_(*conditions)
