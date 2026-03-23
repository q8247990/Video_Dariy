from sqlalchemy import ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class EventTagRel(Base):
    __tablename__ = "event_tag_rel"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("event_record.id"), nullable=False)
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("tag_definition.id"), nullable=False)

    __table_args__ = (
        Index("uk_event_tag_rel", "event_id", "tag_id", unique=True),
        Index("idx_event_tag_rel_tag", "tag_id"),
    )
