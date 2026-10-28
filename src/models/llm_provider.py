from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, validates

from src.core.provider_key_crypto import encrypt_provider_api_key, is_encrypted_provider_api_key
from src.db.base_class import Base


class LLMProvider(Base):
    __tablename__ = "llm_provider"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False, default="qa_provider")
    api_base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    extra_config_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_qa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_tool_calling: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_default_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_default_qa: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_test_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @validates("api_key")
    def _encrypt_api_key(self, key: str, value: str | None) -> str:
        if not value or is_encrypted_provider_api_key(value):
            return value or ""
        return encrypt_provider_api_key(value)

    __table_args__ = (
        Index("idx_llm_provider_type_enabled", "provider_type", "enabled"),
        Index("idx_llm_provider_vision_default", "supports_vision", "is_default_vision", "enabled"),
        Index("idx_llm_provider_qa_default", "supports_qa", "is_default_qa", "enabled"),
    )
