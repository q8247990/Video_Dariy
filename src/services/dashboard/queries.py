from datetime import datetime, timedelta, timezone
from typing import Optional, cast

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.daily_summary import DailySummary
from src.models.home_profile import HomeProfile
from src.models.task_log import TaskLog
from src.models.video_source import VideoSource
from src.services.measures.queries import (
    attention_condition,
    attention_event_count,
    attention_event_rows,
    focus_event_counts,
)
from src.services.pipeline_constants import TaskStatus, TaskType

__all__ = [
    "assistant_name",
    "attention_condition",
    "attention_event_count",
    "attention_event_rows",
    "failed_analysis_count_24h",
    "failed_task_count_24h",
    "focus_event_counts",
    "last_scan_at",
    "latest_daily_summary",
    "latest_task_by_type",
]


def assistant_name(db: Session) -> str:
    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    if profile and (profile.assistant_name or "").strip():
        return profile.assistant_name.strip()
    return ""


def latest_task_by_type(db: Session, task_type: str) -> TaskLog | None:
    return (
        db.query(TaskLog)
        .filter(TaskLog.task_type == task_type)
        .order_by(TaskLog.created_at.desc())
        .first()
    )


def latest_daily_summary(db: Session) -> DailySummary | None:
    return db.query(DailySummary).order_by(DailySummary.summary_date.desc()).first()


def last_scan_at(db: Session) -> datetime | None:
    return cast(Optional[datetime], db.query(func.max(VideoSource.last_scan_at)).scalar())


def failed_analysis_count_24h(db: Session) -> int:
    return (
        db.query(func.count(TaskLog.id))
        .filter(
            TaskLog.task_type == TaskType.SESSION_ANALYSIS,
            TaskLog.status == TaskStatus.FAILED,
            TaskLog.created_at >= datetime.now(timezone.utc) - timedelta(hours=24),
        )
        .scalar()
        or 0
    )


def failed_task_count_24h(db: Session) -> int:
    return (
        db.query(func.count(TaskLog.id))
        .filter(
            TaskLog.status == TaskStatus.FAILED,
            TaskLog.created_at >= datetime.now(timezone.utc) - timedelta(hours=24),
        )
        .scalar()
        or 0
    )
