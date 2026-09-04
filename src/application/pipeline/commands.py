from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class SessionBuildCommand:
    source_id: int
    scan_mode: str = "hot"  # "hot" | "full"


@dataclass(frozen=True)
class AnalyzeSessionCommand:
    session_id: int
    priority: str = "hot"  # "hot" | "full"
    recovery_attempt: int = 0


@dataclass(frozen=True)
class GenerateDailySummaryCommand:
    target_date_str: Optional[str] = None


@dataclass(frozen=True)
class SendWebhookCommand:
    """Command to dispatch a webhook delivery via the outbox.

    ``webhook_id`` scopes delivery to a single subscriber when set;
    ``None`` keeps the legacy fan-out semantics so existing producers
    that have not migrated still work. The canonical
    ``publish_daily_summary`` use case enrolls one outbox row per
    subscriber and always sets ``webhook_id`` so the publisher
    triggers exactly N targeted deliveries (never a duplicate fan-out).
    """

    event_type: str
    payload: dict[str, Any]
    webhook_id: Optional[int] = None
