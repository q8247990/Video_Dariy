import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from src.application.qa.schemas import (
    QUESTION_MODES,
    DateRange,
    EventFilters,
    QueryPlan,
    RetrievalBudgets,
    RetrievalPlan,
    TimeRange,
)
from src.services.video_analysis.enums import IMPORTANCE_LEVELS, VIDEO_EVENT_TYPES

logger = logging.getLogger(__name__)

MAX_LIMIT = 50
DEFAULT_LIMIT = 30
DEFAULT_LOOKBACK_HOURS = 24


# ---------------------------------------------------------------------------
# 意图识别 JSON 解析
# ---------------------------------------------------------------------------


def parse_intent_output(raw_text: str) -> Optional[dict[str, Any]]:
    """从 LLM 返回的文本中提取 JSON 对象。"""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            result, _ = decoder.raw_decode(text[idx:])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    logger.warning("Failed to parse intent output as JSON")
    return None


# ---------------------------------------------------------------------------
# QueryPlan 归一化
# ---------------------------------------------------------------------------


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_query_plan(
    raw: Optional[dict[str, Any]],
    now: datetime,
) -> QueryPlan:
    """将 LLM 输出的原始 dict 归一化为合法的 QueryPlan。

    如果 raw 为 None 或字段非法，使用安全默认值。
    """
    if raw is None:
        return _default_query_plan(now)

    # question_mode
    question_mode = raw.get("question_mode", "overview")
    if question_mode not in QUESTION_MODES:
        question_mode = "overview"

    # time_range
    time_range_raw = raw.get("time_range") or {}
    start = _parse_datetime(time_range_raw.get("start"))
    end = _parse_datetime(time_range_raw.get("end"))
    time_label = str(time_range_raw.get("time_label", ""))

    if start and end:
        if start > end:
            start, end = end, start
        # 去掉时区信息以统一比较
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
        time_range = TimeRange(start=start, end=end, time_label=time_label)
    else:
        time_range = TimeRange(
            start=now - timedelta(hours=DEFAULT_LOOKBACK_HOURS),
            end=now,
            time_label="last_24_hours",
        )

    # subjects
    subjects_raw = raw.get("subjects") or []
    subjects = [str(s).strip() for s in subjects_raw if isinstance(s, str) and s.strip()]

    # event_types
    event_types_raw = raw.get("event_types") or []
    event_types = [
        str(t).strip()
        for t in event_types_raw
        if isinstance(t, str) and t.strip() in VIDEO_EVENT_TYPES
    ]

    # importance_levels
    importance_raw = raw.get("importance_levels") or []
    importance_levels = [
        str(i).strip()
        for i in importance_raw
        if isinstance(i, str) and i.strip() in IMPORTANCE_LEVELS
    ]

    # booleans
    use_daily_summary_first = bool(raw.get("use_daily_summary_first", True))
    use_session_summary_first = bool(raw.get("use_session_summary_first", True))
    need_event_details = bool(raw.get("need_event_details", True))

    # limit
    limit_raw = raw.get("limit", DEFAULT_LIMIT)
    try:
        limit = min(max(int(limit_raw), 1), MAX_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    return QueryPlan(
        question_mode=question_mode,
        time_range=time_range,
        subjects=subjects,
        event_types=event_types,
        importance_levels=importance_levels,
        use_daily_summary_first=use_daily_summary_first,
        use_session_summary_first=use_session_summary_first,
        need_event_details=need_event_details,
        limit=limit,
    )


def _default_query_plan(now: datetime) -> QueryPlan:
    return QueryPlan(
        question_mode="overview",
        time_range=TimeRange(
            start=now - timedelta(hours=DEFAULT_LOOKBACK_HOURS),
            end=now,
            time_label="last_24_hours",
        ),
        use_daily_summary_first=True,
        use_session_summary_first=True,
        need_event_details=True,
        limit=DEFAULT_LIMIT,
    )


# ---------------------------------------------------------------------------
# 检索规划
# ---------------------------------------------------------------------------


def build_retrieval_plan(query_plan: QueryPlan) -> RetrievalPlan:
    """根据归一化后的 QueryPlan 生成 RetrievalPlan。"""
    assert query_plan.time_range is not None

    tr = query_plan.time_range
    time_span_hours = (tr.end - tr.start).total_seconds() / 3600

    # 是否查日报
    load_daily = query_plan.use_daily_summary_first and time_span_hours >= 24

    # 是否查 session
    load_sessions = query_plan.use_session_summary_first or query_plan.question_mode in {
        "overview",
        "risk_check",
        "subject_activity",
    }

    # 是否查 event
    load_events = query_plan.need_event_details

    # 日报日期范围
    daily_date_range: Optional[DateRange] = None
    if load_daily:
        daily_date_range = DateRange(
            start_date=tr.start.date(),
            end_date=tr.end.date(),
        )

    # session / event 时间范围复用 query_plan 的 time_range
    session_time_range = TimeRange(start=tr.start, end=tr.end) if load_sessions else None
    event_time_range = TimeRange(start=tr.start, end=tr.end) if load_events else None

    # event 过滤条件
    event_filters = EventFilters(
        subjects=list(query_plan.subjects),
        event_types=list(query_plan.event_types),
        importance_levels=list(query_plan.importance_levels),
    )

    # 预算
    budgets = _compute_budgets(query_plan, time_span_hours)

    return RetrievalPlan(
        load_daily_summaries=load_daily,
        load_sessions=load_sessions,
        load_events=load_events,
        daily_summary_date_range=daily_date_range,
        session_time_range=session_time_range,
        event_time_range=event_time_range,
        event_filters=event_filters,
        budgets=budgets,
    )


def _compute_budgets(query_plan: QueryPlan, time_span_hours: float) -> RetrievalBudgets:
    mode = query_plan.question_mode

    if mode == "latest":
        return RetrievalBudgets(max_daily_summaries=1, max_sessions=3, max_events=5)

    if mode == "existence":
        return RetrievalBudgets(max_daily_summaries=2, max_sessions=6, max_events=20)

    if mode == "risk_check":
        return RetrievalBudgets(max_daily_summaries=3, max_sessions=10, max_events=30)

    if mode == "subject_activity":
        return RetrievalBudgets(max_daily_summaries=2, max_sessions=8, max_events=30)

    # overview
    if time_span_hours >= 72:
        return RetrievalBudgets(max_daily_summaries=3, max_sessions=8, max_events=20)
    if time_span_hours >= 24:
        return RetrievalBudgets(max_daily_summaries=3, max_sessions=12, max_events=30)
    return RetrievalBudgets(max_daily_summaries=1, max_sessions=8, max_events=40)
