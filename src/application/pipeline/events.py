from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SessionSealed:
    session_id: int
    source_id: int
    priority: str  # "hot" | "full"


@dataclass(frozen=True)
class SessionAnalyzed:
    session_id: int
    source_id: int
    events_created: int
    chunk_count: int
    analyzed_at: datetime
    analysis_status: str = "success"
