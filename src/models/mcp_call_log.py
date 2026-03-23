from typing import Any, Optional

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class McpCallLog(Base):
    __tablename__ = "mcp_call_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    request_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    response_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="success", nullable=False)

    __table_args__ = (Index("idx_mcp_call_log_tool_created", "tool_name", "created_at"),)
