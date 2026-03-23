from typing import Optional

from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class TagDefinition(Base):
    __tablename__ = "tag_definition"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tag_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tag_type: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("uk_tag_definition_name_type", "tag_name", "tag_type", unique=True),
        Index("idx_tag_definition_enabled", "enabled"),
    )
