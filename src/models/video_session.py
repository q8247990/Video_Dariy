from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class VideoSession(Base):
    __tablename__ = "video_session"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("video_source.id"), nullable=False)
    session_start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    session_end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    total_duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    merge_rule: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    summary_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    activity_level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    main_subjects_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    has_important_event: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    analysis_notes_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    last_analyzed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    analysis_priority: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        Index("idx_video_session_source_start", "source_id", "session_start_time"),
        Index("idx_video_session_status", "analysis_status"),
        Index(
            "idx_video_session_source_status_end",
            "source_id",
            "analysis_status",
            "session_end_time",
        ),
        Index("idx_video_session_priority", "analysis_priority", "analysis_status"),
    )
