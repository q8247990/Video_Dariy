from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from src.application.qa.schemas import CompressedEvidence


@dataclass
class VideoSourcePromptContext:
    source_name: str
    camera_name: str
    location_name: str
    prompt_text: Optional[str] = None
    source_type: str = "local_directory"


@dataclass
class SessionPromptContext:
    session_id: int
    source_id: int
    session_start_time: datetime
    session_end_time: datetime
    total_duration_seconds: Optional[int] = None
    segment_index: int = 0
    segment_start_offset_sec: int = 0
    segment_duration_seconds: int = 0


@dataclass
class StrategyPromptContext:
    ingest_type: str
    source_type: str
    strategy_note: str


@dataclass
class VideoRecognitionPromptInput:
    home_context: dict[str, Any]
    video_source: VideoSourcePromptContext
    session_context: SessionPromptContext
    strategy_context: StrategyPromptContext
    event_type_list: list[str] = field(default_factory=list)


@dataclass
class DailySummaryPromptInput:
    home_context: dict[str, Any]
    summary_date: date
    time_range_start: str
    time_range_end: str
    subject_sections: list[dict[str, Any]] = field(default_factory=list)
    missing_subjects: list[str] = field(default_factory=list)
    attention_candidates: list[dict[str, Any]] = field(default_factory=list)
    locale: str | None = None


@dataclass
class QAIntentPromptInput:
    question: str
    now: datetime
    timezone: str
    home_context: dict[str, Any]
    locale: str | None = None


@dataclass
class QAAnswerPromptInput:
    question: str
    now_iso: str
    timezone: str
    home_context_text: str
    evidence: CompressedEvidence
    locale: str | None = None
