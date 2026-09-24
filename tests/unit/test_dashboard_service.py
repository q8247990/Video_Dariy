from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.dashboard import get_dashboard_overview
from src.services.home_timezone import local_day_bounds
from src.services.summarizer.schedule import get_home_timezone


def _create_source_and_session(pg_db: Session, event_time: datetime) -> tuple[int, int]:
    source = VideoSource(
        source_name="source-1",
        camera_name="客厅",
        location_name="客厅",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()

    session = VideoSession(
        source_id=source.id,
        session_start_time=event_time,
        session_end_time=event_time + timedelta(minutes=30),
    )
    pg_db.add(session)
    pg_db.flush()
    return source.id, session.id


def test_event_summary_counts_attention_events_and_focus(pg_db: Session) -> None:
    zone = get_home_timezone(pg_db)
    day_start, _ = local_day_bounds(zone, datetime.now(zone).date())
    event_time = day_start + timedelta(hours=1)
    source_id, session_id = _create_source_and_session(pg_db, event_time)

    pg_db.add(
        EventRecord(
            source_id=source_id,
            session_id=session_id,
            event_start_time=event_time,
            description="门口出现陌生人",
            event_type="unknown_person_appear",
        )
    )
    pg_db.add(
        EventRecord(
            source_id=source_id,
            session_id=session_id,
            event_start_time=event_time,
            description="成员在客厅停留",
            event_type="member_stay",
            focus_matches_json=["pet_status"],
        )
    )
    pg_db.commit()

    overview = get_dashboard_overview(pg_db)

    assert overview.event_summary.attention_event_count == 1
    focus_counts = {item.focus_key: item.count for item in overview.event_summary.focus_counts}
    assert focus_counts.get("pet_status") == 1


def test_attention_events_list_is_newest_first(pg_db: Session) -> None:
    zone = get_home_timezone(pg_db)
    day_start, _ = local_day_bounds(zone, datetime.now(zone).date())
    event_time = day_start + timedelta(hours=1)
    source_id, session_id = _create_source_and_session(pg_db, event_time)

    pg_db.add(
        EventRecord(
            source_id=source_id,
            session_id=session_id,
            event_start_time=event_time,
            description="陌生人-较早",
            event_type="unknown_person_appear",
        )
    )
    pg_db.add(
        EventRecord(
            source_id=source_id,
            session_id=session_id,
            event_start_time=event_time + timedelta(minutes=10),
            description="陌生人-较新",
            event_type="unknown_person_appear",
        )
    )
    pg_db.add(
        EventRecord(
            source_id=source_id,
            session_id=session_id,
            event_start_time=event_time + timedelta(minutes=20),
            description="成员停留不应进入需关注列表",
            event_type="member_stay",
        )
    )
    pg_db.commit()

    overview = get_dashboard_overview(pg_db, locale="zh-CN")

    attention_events = overview.attention_events
    assert len(attention_events) == 2
    assert attention_events[0].summary == "陌生人-较新"
    assert attention_events[1].summary == "陌生人-较早"


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

    overview = get_dashboard_overview(pg_db)

    latest = overview.latest_daily_summary
    assert latest.exists is True
    assert str(latest.date) == "2026-03-13"
    assert latest.status == "success"
