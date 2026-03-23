from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class TaskLog(Base):
    __tablename__ = "task_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    task_target_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    queue_task_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    detail_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_task_log_status_type", "status", "task_type"),
        Index("idx_task_log_created_at", "created_at"),
        Index(
            "idx_task_log_target_type_status_created",
            "task_target_id",
            "task_type",
            "status",
            "created_at",
        ),
        Index("idx_task_log_type_target_status", "task_type", "task_target_id", "status"),
    )
