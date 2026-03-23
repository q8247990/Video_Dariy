from typing import Any, Optional

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class AppRuntimeState(Base):
    __tablename__ = "app_runtime_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    state_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    state_value: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
