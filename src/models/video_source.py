from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class VideoSource(Base):
    __tablename__ = "video_source"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    camera_name: Mapped[str] = mapped_column(String(128), nullable=False)
    location_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    analyze_from_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_scan_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_validate_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_validate_message: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    last_validate_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
