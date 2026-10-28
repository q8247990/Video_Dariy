from typing import Any

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class HomeProfile(Base):
    __tablename__ = "home_profile"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    home_name: Mapped[str] = mapped_column(String(128), nullable=False)
    family_tags_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    focus_points_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    system_style: Mapped[str] = mapped_column(String(32), nullable=False)
    style_preference_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    assistant_name: Mapped[str] = mapped_column(String(128), nullable=False)
    home_note: Mapped[str | None] = mapped_column(Text, nullable=True)
