from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base_class import Base

if TYPE_CHECKING:
    from src.models.video_source import VideoSource


class VideoSourceRuntimeState(Base):
    __tablename__ = "video_source_runtime_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("video_source.id"), nullable=False, unique=True
    )

    latency_alert_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_alert_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_alert_last_notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    source: Mapped["VideoSource"] = relationship("VideoSource", back_populates="runtime_state")

    __table_args__ = (Index("idx_video_source_runtime_state_source", "source_id", unique=True),)
