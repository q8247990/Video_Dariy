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


@dataclass(frozen=True)
class GenerateDailySummaryCommand:
    target_date_str: Optional[str] = None


@dataclass(frozen=True)
class SendWebhookCommand:
    event_type: str
    payload: dict[str, Any]
