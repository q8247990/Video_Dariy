"""Single source of truth for business statistic definitions.

Each measure declares its key, meaning, dimensions and source table so the
dashboard / QA / daily-summary consumers share one vocabulary instead of
re-deriving "event count" / "attention" locally. Operational health
gauges (outbox, lease recovery, token usage) stay in their own modules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Measure:
    key: str
    description: str
    dimensions: tuple[str, ...]
    source: str


MEASURES: dict[str, Measure] = {
    "presence": Measure(
        key="presence",
        description="主体在家庭本地日的在场/缺席",
        dimensions=("home_local_day", "subject"),
        source="event_record",
    ),
    "subject_activity": Measure(
        key="subject_activity",
        description="主体活动量与时间线",
        dimensions=("home_local_day", "subject"),
        source="event_record",
    ),
    "focus_events": Measure(
        key="focus_events",
        description="按用户关注项计数的事件",
        dimensions=("home_local_day", "focus_key"),
        source="event_record",
    ),
    "attention_events": Measure(
        key="attention_events",
        description="规则判定的需关注事件",
        dimensions=("home_local_day",),
        source="event_record",
    ),
    "analysis_health": Measure(
        key="analysis_health",
        description="分析状态分布与覆盖率",
        dimensions=("home_local_day", "status"),
        source="video_session",
    ),
    "llm_tokens": Measure(
        key="llm_tokens",
        description="LLM token 消耗",
        dimensions=("home_local_day", "provider", "scene"),
        source="llm_usage_log",
    ),
}


__all__ = ["MEASURES", "Measure"]
