from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class EventEvidence:
    id: int
    session_id: int
    event_start_time: datetime
    event_type: str = ""
    attention: bool = False
    title: str = ""
    summary: str = ""
    detail: str = ""
    related_entities: list[dict[str, Any]] = field(default_factory=list)
    observed_actions: list[str] = field(default_factory=list)
    interpreted_state: list[str] = field(default_factory=list)


@dataclass
class SessionEvidence:
    id: int
    session_start_time: datetime
    session_end_time: datetime
    summary_text: str = ""
    activity_level: str = ""
    main_subjects: list[str] = field(default_factory=list)
    has_attention_event: bool = False
    analysis_notes: list[dict[str, Any]] = field(default_factory=list)


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
    referenced_events: list[EventEvidence] = field(default_factory=list)
    referenced_sessions: list[SessionEvidence] = field(default_factory=list)
    provider_id: Optional[int] = None
