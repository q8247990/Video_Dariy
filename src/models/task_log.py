from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base

#: Literal predicate of the partial unique index
#: ``ux_task_log_active_dedupe_key``. Used verbatim as the
#: ``ON CONFLICT … WHERE`` arbiter in the dispatch claim / worker-bind
#: INSERTs.
#:
#: **Must stay plain SQL with literal constants — no bind parameters.**
#: PostgreSQL's plan cache switches a long-lived prepared statement to a
#: *generic* plan after ~10 executions inside one transaction, and a
#: parameterized arbiter predicate cannot be matched against the index
#: predicate under a generic plan — PostgreSQL then raises
#: ``InvalidColumnReference: there is no unique or exclusion constraint
#: matching the ON CONFLICT specification`` (see ADR 0012 incident:
#: 775-iteration dispatch loop aborted mid-transaction).
ACTIVE_DEDUPE_INDEX_PREDICATE = "dedupe_key IS NOT NULL AND status IN ('pending', 'running')"


class TaskLog(Base):
    __tablename__ = "task_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    task_target_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    queue_task_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recovery_attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ``Text`` (not ``String(512)``): terminal failures store ``str(exc)``
    # which includes the full SQL + bound parameters and can easily exceed
    # 512 chars; truncating it would lose the diagnostic detail, and a
    # fixed varchar(N) would just overflow again on a larger payload.
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
