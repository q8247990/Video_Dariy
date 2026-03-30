"""共享查询层数据结构。

QA Agent 和 MCP 共用的查询参数与返回结果定义。
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 查询参数
# ---------------------------------------------------------------------------


@dataclass
class TimeRange:
    start: datetime
    end: datetime


@dataclass
class DateRange:
    start_date: date
    end_date: date


@dataclass
class EventFilters:
    subjects: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    importance_levels: list[str] = field(default_factory=list)
    limit: int = 20


# ---------------------------------------------------------------------------
# 返回结果
# ---------------------------------------------------------------------------


@dataclass
class DataAvailability:
    earliest_event_date: Optional[date] = None
    latest_event_date: Optional[date] = None
    total_event_count: int = 0
    video_sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EventResult:
    id: int
    event_start_time: datetime
    event_type: str = ""
    importance_level: str = ""
    title: str = ""
    summary: str = ""
    subjects: list[str] = field(default_factory=list)


@dataclass
class SessionResult:
    id: int
    session_start_time: datetime
    session_end_time: datetime
    summary_text: str = ""
    activity_level: str = ""
    main_subjects: list[str] = field(default_factory=list)
    has_important_event: bool = False


@dataclass
class DailySummaryResult:
    date: date
    summary_title: str = ""
    overall_summary: str = ""
    subject_sections: list[dict[str, Any]] = field(default_factory=list)
    attention_items: list[dict[str, Any]] = field(default_factory=list)
    event_count: int = 0
