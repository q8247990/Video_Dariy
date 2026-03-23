from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.task_log import TaskLog
from src.models.video_source import VideoSource
from src.services.dashboard import (
    _build_event_summary,
    _build_important_events,
    _build_latest_daily_summary,
)


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    EventRecord.__table__.create(bind=engine)
    DailySummary.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def test_important_event_count_uses_high_only() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="source-1",
            camera_name="客厅",
            location_name="客厅",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.flush()

        now = datetime.utcnow()
        high_event = EventRecord(
            source_id=source.id,
            session_id=1,
            event_start_time=now - timedelta(hours=1),
            event_end_time=now - timedelta(hours=1) + timedelta(minutes=1),
            description="高优先级事件",
            importance_level="high",
        )
        medium_event = EventRecord(
            source_id=source.id,
            session_id=1,
            event_start_time=now - timedelta(hours=2),
            event_end_time=now - timedelta(hours=2) + timedelta(minutes=1),
            description="包含 intrusion 关键词但仅 medium",
            action_type="intrusion",
            importance_level="medium",
        )
        no_importance_event = EventRecord(
            source_id=source.id,
            session_id=1,
            event_start_time=now - timedelta(hours=3),
            event_end_time=now - timedelta(hours=3) + timedelta(minutes=1),
            description="包含 告警 关键词但无重要级别",
            action_type="告警",
            importance_level=None,
        )
        db.add(high_event)
        db.add(medium_event)
        db.add(no_importance_event)
        db.commit()

        summary = _build_event_summary(db)
        assert summary.important_event_count_24h == 1
    finally:
        db.close()


def test_important_events_list_filters_to_high_only() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="source-1",
            camera_name="玄关",
            location_name="玄关",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.flush()

        now = datetime.utcnow()
        event_high_old = EventRecord(
            source_id=source.id,
            session_id=1,
            event_start_time=now - timedelta(hours=2),
            event_end_time=now - timedelta(hours=2) + timedelta(minutes=1),
            description="高优先级-较早",
            importance_level="high",
        )
        event_high_new = EventRecord(
            source_id=source.id,
            session_id=1,
            event_start_time=now - timedelta(hours=1),
            event_end_time=now - timedelta(hours=1) + timedelta(minutes=1),
            description="高优先级-较新",
            importance_level="high",
        )
        event_medium = EventRecord(
            source_id=source.id,
            session_id=1,
            event_start_time=now - timedelta(minutes=30),
            event_end_time=now - timedelta(minutes=29),
            description="中优先级不应进入重点列表",
            importance_level="medium",
        )
        db.add(event_high_old)
        db.add(event_high_new)
        db.add(event_medium)
        db.commit()

        important_events = _build_important_events(db)

        assert len(important_events) == 2
        assert important_events[0].summary == "高优先级-较新"
        assert important_events[1].summary == "高优先级-较早"
    finally:
        db.close()


def test_latest_daily_summary_returns_date_value() -> None:
    db = _new_db_session()
    try:
        db.add(
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
        db.commit()

        latest = _build_latest_daily_summary(db)

        assert latest.exists is True
        assert str(latest.date) == "2026-03-13"
        assert latest.status == "success"
    finally:
        db.close()
