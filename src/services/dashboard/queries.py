from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.home_profile import HomeProfile
from src.models.task_log import TaskLog
from src.models.video_source import VideoSource
from src.services.pipeline_constants import TaskStatus, TaskType

IMPORTANT_LEVELS = ("high",)


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


def event_summary_counts(db: Session) -> tuple[int, int, int]:
    now = datetime.now(timezone.utc)
    today_start = datetime.combine(now.date(), datetime.min.time())
    yesterday_start = today_start - timedelta(days=1)

    today_event_count = (
        db.query(func.count(EventRecord.id))
        .filter(EventRecord.event_start_time >= today_start, EventRecord.event_start_time <= now)
        .scalar()
        or 0
    )

    yesterday_event_count = (
        db.query(func.count(EventRecord.id))
        .filter(
            EventRecord.event_start_time >= yesterday_start,
            EventRecord.event_start_time < today_start,
        )
        .scalar()
        or 0
    )

    important_event_count_24h = (
        db.query(func.count(EventRecord.id))
        .filter(
            EventRecord.event_start_time >= now - timedelta(hours=24),
            _important_condition(),
        )
        .scalar()
        or 0
    )

    return today_event_count, yesterday_event_count, important_event_count_24h


def important_event_rows(db: Session) -> list[tuple[EventRecord, VideoSource]]:
    importance_score = case(
        (EventRecord.importance_level == "high", 3),
        else_=0,
    )

    rows = (
        db.query(EventRecord, VideoSource)
        .join(VideoSource, EventRecord.source_id == VideoSource.id)
        .filter(_important_condition())
        .order_by(importance_score.desc(), EventRecord.event_start_time.desc())
        .limit(5)
        .all()
    )
    return cast(list[tuple[EventRecord, VideoSource]], rows)


def _important_condition() -> Any:
    return EventRecord.importance_level.in_(IMPORTANT_LEVELS)
