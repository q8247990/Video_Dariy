from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DECIMAL, JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class EventRecord(Base):
    __tablename__ = "event_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("video_source.id"), nullable=False)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("video_session.id"), nullable=False)
    event_start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    event_end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    object_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    action_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[Optional[float]] = mapped_column(DECIMAL(5, 4), nullable=True)
    event_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    importance_level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    offset_start_sec: Mapped[Optional[float]] = mapped_column(DECIMAL(10, 3), nullable=True)
    offset_end_sec: Mapped[Optional[float]] = mapped_column(DECIMAL(10, 3), nullable=True)
    related_entities_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    observed_actions_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    interpreted_state_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    raw_result: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_event_source_start", "source_id", "event_start_time"),
        Index("idx_event_session_id", "session_id"),
        Index("idx_event_object_type", "object_type"),
        Index("idx_event_action_type", "action_type"),
    )
