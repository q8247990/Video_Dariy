from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class TaskDelivery(Base):
    __tablename__ = "task_delivery"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_log_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    args_json: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    kwargs_json: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["TaskDelivery"]
