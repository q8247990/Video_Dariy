from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class VideoSourcePromptContext:
    source_name: str
    camera_name: str
    location_name: str
    prompt_text: str | None = None
    source_type: str = "local_directory"


@dataclass
class SessionPromptContext:
    session_id: int
    source_id: int
    session_start_time: datetime
    session_end_time: datetime
    total_duration_seconds: int | None = None
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
    input_mode: str = "video"
    frame_timestamps: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 与编译器校验保持一致：image 模式必须有非空帧时间戳，防止绕过编译器静默渲染空区段。
        if self.input_mode == "image" and not self.frame_timestamps:
            raise ValueError("image input mode requires at least one frame timestamp")


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
