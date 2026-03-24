from typing import Any, Optional

from sqlalchemy import JSON, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class ChatQueryLog(Base):
    __tablename__ = "chat_query_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_question: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_condition_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    referenced_event_ids_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    provider_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("llm_provider.id", ondelete="SET NULL"), nullable=True
    )
    provider_name_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_chat_query_log_created_at", "created_at"),)
