"""Shared query-layer data structures.

Query parameters and result shapes used by both the QA agent and MCP.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

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
    attention: bool | None = None
    limit: int = 20


# ---------------------------------------------------------------------------
# 返回结果
# ---------------------------------------------------------------------------


@dataclass
class DataAvailability:
    earliest_event_date: date | None = None
    latest_event_date: date | None = None
    total_event_count: int = 0
    video_sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EventResult:
    id: int
    event_start_time: datetime
    event_type: str = ""
    attention: bool = False
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
    has_attention_event: bool = False


@dataclass
class DailySummaryResult:
    date: date
    summary_title: str = ""
    overall_summary: str = ""
    subject_sections: list[dict[str, Any]] = field(default_factory=list)
    attention_items: list[dict[str, Any]] = field(default_factory=list)
    event_count: int = 0
