from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# question_mode 枚举
# ---------------------------------------------------------------------------
QUESTION_MODES = {"overview", "latest", "existence", "subject_activity", "risk_check"}

# ---------------------------------------------------------------------------
# 意图识别输出：QueryPlan
# ---------------------------------------------------------------------------


@dataclass
class TimeRange:
    start: datetime
    end: datetime
    time_label: str = ""


@dataclass
class QueryPlan:
    question_mode: str = "overview"
    time_range: Optional[TimeRange] = None
    subjects: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    importance_levels: list[str] = field(default_factory=list)
    use_daily_summary_first: bool = True
    use_session_summary_first: bool = True
    need_event_details: bool = True
    limit: int = 30


# ---------------------------------------------------------------------------
# 检索规划：RetrievalPlan
# ---------------------------------------------------------------------------


@dataclass
class DateRange:
    start_date: date
    end_date: date


@dataclass
class RetrievalBudgets:
    max_daily_summaries: int = 3
    max_sessions: int = 12
    max_events: int = 40


@dataclass
class EventFilters:
    subjects: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    importance_levels: list[str] = field(default_factory=list)


@dataclass
class RetrievalPlan:
    load_daily_summaries: bool = True
    load_sessions: bool = True
    load_events: bool = True
    daily_summary_date_range: Optional[DateRange] = None
    session_time_range: Optional[TimeRange] = None
    event_time_range: Optional[TimeRange] = None
    event_filters: EventFilters = field(default_factory=EventFilters)
    budgets: RetrievalBudgets = field(default_factory=RetrievalBudgets)


# ---------------------------------------------------------------------------
# 证据层数据结构
# ---------------------------------------------------------------------------


@dataclass
class DailySummaryEvidence:
    summary_date: date
    overall_summary: str
    subject_sections: list[dict[str, Any]] = field(default_factory=list)
    attention_items: list[dict[str, Any]] = field(default_factory=list)
    event_count: int = 0


@dataclass
class SessionEvidence:
    id: int
    session_start_time: datetime
    session_end_time: datetime
    summary_text: str = ""
    activity_level: str = ""
    main_subjects: list[str] = field(default_factory=list)
    has_important_event: bool = False
    analysis_notes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EventEvidence:
    id: int
    session_id: int
    event_start_time: datetime
    event_type: str = ""
    importance_level: str = ""
    title: str = ""
    summary: str = ""
    detail: str = ""
    related_entities: list[dict[str, Any]] = field(default_factory=list)
    observed_actions: list[str] = field(default_factory=list)
    interpreted_state: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 压缩后的证据集合
# ---------------------------------------------------------------------------


@dataclass
class CompressedEvidence:
    home_context_text: str = ""
    query_plan_text: str = ""
    daily_summary_text: str = ""
    session_text: str = ""
    event_text: str = ""


# ---------------------------------------------------------------------------
# QA 服务输入输出
# ---------------------------------------------------------------------------


@dataclass
class QARequest:
    question: str
    now: datetime
    timezone: str = "Asia/Shanghai"
    write_query_log: bool = True
    request_source: str = "web"
    locale: str = "zh-CN"


@dataclass
class QAResult:
    question: str
    answer_text: str
    query_plan: Optional[QueryPlan] = None
    referenced_events: list[EventEvidence] = field(default_factory=list)
    referenced_sessions: list[SessionEvidence] = field(default_factory=list)
    referenced_daily_summaries: list[DailySummaryEvidence] = field(default_factory=list)
    provider_id: Optional[int] = None
