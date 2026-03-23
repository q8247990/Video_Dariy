from typing import Any, Optional

from sqlalchemy import JSON, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class WebhookConfig(Base):
    __tablename__ = "webhook_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    headers_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    event_types_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    event_subscriptions_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
