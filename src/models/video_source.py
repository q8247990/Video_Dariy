from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Date, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base_class import Base

if TYPE_CHECKING:
    from src.models.video_file import VideoFile
    from src.models.video_session import VideoSession


class VideoSource(Base):
    __tablename__ = "video_source"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    camera_name: Mapped[str] = mapped_column(String(128), nullable=False)
    location_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analyze_from_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_validate_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_validate_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_validate_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    video_files: Mapped[list["VideoFile"]] = relationship(
        "VideoFile", back_populates="source", lazy="select", passive_deletes=True
    )
    sessions: Mapped[list["VideoSession"]] = relationship(
        "VideoSession", back_populates="source", lazy="select", passive_deletes=True
    )
