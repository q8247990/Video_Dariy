from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base_class import Base


class DailySummary(Base):
    __tablename__ = "daily_summary"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    summary_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    summary_title: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    overall_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subject_sections_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    attention_items_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("llm_provider.id"), nullable=True
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
