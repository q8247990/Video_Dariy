from typing import Optional

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class HomeEntityProfile(Base):
    __tablename__ = "home_entity_profile"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role_type: Mapped[str] = mapped_column(String(64), nullable=False)
    age_group: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    breed: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    appearance_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    personality_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    image_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
