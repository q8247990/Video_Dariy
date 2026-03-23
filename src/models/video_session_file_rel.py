from sqlalchemy import ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class VideoSessionFileRel(Base):
    __tablename__ = "video_session_file_rel"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("video_session.id"), nullable=False)
    video_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("video_file.id"), nullable=False)
    sort_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("uk_session_file_rel", "session_id", "video_file_id", unique=True),
        Index("idx_session_file_rel_file", "video_file_id"),
    )
