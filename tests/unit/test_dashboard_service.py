from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.dashboard import (
    _build_event_summary,
    _build_important_events,
    _build_latest_daily_summary,
)


def test_important_event_count_uses_high_only(pg_db: Session) -> None:
    source = VideoSource(
        source_name="source-1",
        camera_name="客厅",
        location_name="客厅",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()

    pg_db.add(source)
    pg_db.flush()

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime.utcnow() - timedelta(hours=4),
        session_end_time=datetime.utcnow(),
    )
    pg_db.add(session)
    pg_db.flush()

    now = datetime.utcnow()
    high_event = EventRecord(
        source_id=source.id,
        session_id=session.id,
        event_start_time=now - timedelta(hours=1),
        event_end_time=now - timedelta(hours=1) + timedelta(minutes=1),
        description="高优先级事件",
        importance_level="high",
    )
    medium_event = EventRecord(
        source_id=source.id,
        session_id=session.id,
        event_start_time=now - timedelta(hours=2),
        event_end_time=now - timedelta(hours=2) + timedelta(minutes=1),
        description="包含 intrusion 关键词但仅 medium",
        action_type="intrusion",
        importance_level="medium",
    )
    no_importance_event = EventRecord(
        source_id=source.id,
        session_id=session.id,
        event_start_time=now - timedelta(hours=3),
        event_end_time=now - timedelta(hours=3) + timedelta(minutes=1),
        description="包含 告警 关键词但无重要级别",
        action_type="告警",
        importance_level=None,
    )
    pg_db.add(high_event)
    pg_db.add(medium_event)
    pg_db.add(no_importance_event)
    pg_db.commit()

    summary = _build_event_summary(pg_db)
    assert summary.important_event_count_24h == 1


def test_important_events_list_filters_to_high_only(pg_db: Session) -> None:
    source = VideoSource(
        source_name="source-1",
        camera_name="玄关",
        location_name="玄关",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime.utcnow() - timedelta(hours=4),
        session_end_time=datetime.utcnow(),
    )
    pg_db.add(session)
    pg_db.flush()

    now = datetime.utcnow()
    event_high_old = EventRecord(
        source_id=source.id,
        session_id=session.id,
        event_start_time=now - timedelta(hours=2),
        event_end_time=now - timedelta(hours=2) + timedelta(minutes=1),
        description="高优先级-较早",
        importance_level="high",
    )
    event_high_new = EventRecord(
        source_id=source.id,
        session_id=session.id,
        event_start_time=now - timedelta(hours=1),
        event_end_time=now - timedelta(hours=1) + timedelta(minutes=1),
        description="高优先级-较新",
        importance_level="high",
    )
    event_medium = EventRecord(
        source_id=source.id,
        session_id=session.id,
        event_start_time=now - timedelta(minutes=30),
        event_end_time=now - timedelta(minutes=29),
        description="中优先级不应进入重点列表",
        importance_level="medium",
    )
    pg_db.add(event_high_old)
    pg_db.add(event_high_new)
    pg_db.add(event_medium)
    pg_db.commit()

    important_events = _build_important_events(pg_db, "zh-CN")

    assert len(important_events) == 2
    assert important_events[0].summary == "高优先级-较新"
    assert important_events[1].summary == "高优先级-较早"


def test_latest_daily_summary_returns_date_value(pg_db: Session) -> None:
    pg_db.add(
        DailySummary(
            summary_date=datetime(2026, 3, 13).date(),
            summary_title="2026-03-13 家庭日报",
            overall_summary="昨天家中整体平稳。",
            subject_sections_json=[],
            attention_items_json=[],
            event_count=1,
            generated_at=datetime(2026, 3, 14, 9, 0, 0),
        )
    )
    pg_db.commit()

    latest = _build_latest_daily_summary(pg_db)

    assert latest.exists is True
    assert str(latest.date) == "2026-03-13"
    assert latest.status == "success"
