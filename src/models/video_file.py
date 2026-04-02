import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base_class import Base

if TYPE_CHECKING:
    from src.models.video_source import VideoSource


def build_file_path_hash(file_path: str) -> str:
    return hashlib.sha256(file_path.encode("utf-8")).hexdigest()


def _default_file_path_hash(context: Any) -> str:
    params = context.get_current_parameters()
    file_path = str(params.get("file_path") or "")
    return build_file_path_hash(file_path)


class VideoFile(Base):
    __tablename__ = "video_file"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("video_source.id"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_path_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=_default_file_path_hash,
    )
    storage_type: Mapped[str] = mapped_column(String(32), default="local_file", nullable=False)
    access_uri: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    file_format: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    parse_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    parse_message: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    source: Mapped["VideoSource"] = relationship("VideoSource", back_populates="video_files")

    __table_args__ = (
        Index("uk_video_file_source_path_hash", "source_id", "file_path_hash", unique=True),
        Index("idx_video_file_source_start", "source_id", "start_time"),
        Index("idx_video_file_parse_status", "parse_status"),
    )
