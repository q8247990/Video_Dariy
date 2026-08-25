from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class SessionAnalysisCheckpoint(Base):
    __tablename__ = "session_analysis_checkpoint"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("video_session.id", ondelete="CASCADE"), nullable=False
    )
    analysis_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sub_chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    event_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "analysis_run_id",
            "chunk_index",
            "sub_chunk_index",
            name="uq_session_analysis_checkpoint_work",
        ),
        Index("idx_session_analysis_checkpoint_session_run", "session_id", "analysis_run_id"),
        Index("idx_session_analysis_checkpoint_state", "state"),
    )
