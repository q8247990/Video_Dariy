from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from src.api.v1.endpoints.daily_summaries import generate_all_daily_summaries, get_daily_summary
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType


def _current_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


def _seed_source_and_session(
    pg_db: Session,
    *,
    session_start: datetime,
    session_end: datetime,
    analysis_status: SessionAnalysisStatus = SessionAnalysisStatus.SUCCESS,
) -> tuple[VideoSource, VideoSession]:
    source = VideoSource(
        source_name="src-daily",
        camera_name="cam-daily",
        location_name="daily-test",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=session_start,
        session_end_time=session_end,
        total_duration_seconds=int((session_end - session_start).total_seconds()),
        analysis_status=analysis_status,
    )
    pg_db.add(session)
    pg_db.flush()
    return source, session


def test_get_daily_summary_contains_detail_text(pg_db: Session) -> None:
    target_date = datetime(2026, 3, 14)
    pg_db.add(
        DailySummary(
            summary_date=target_date.date(),
            summary_title="2026-03-14 家庭日报",
            overall_summary="昨天整体平稳。",
            subject_sections_json=[
                {
                    "subject_name": "爸爸",
                    "subject_type": "member",
                    "summary": "上午在客厅活动较多。",
                    "attention_needed": False,
                }
            ],
            attention_items_json=[
                {
                    "title": "门口短暂停留",
                    "summary": "傍晚门口有短暂停留，建议留意。",
                    "level": "low",
                }
            ],
            event_count=3,
            generated_at=datetime(2026, 3, 15, 8, 0, 0),
        )
    )

    source, session = _seed_source_and_session(
        pg_db,
        session_start=datetime(2026, 3, 14, 8, 0, 0),
        session_end=datetime(2026, 3, 14, 8, 30, 0),
    )
    pg_db.add_all(
        [
            EventRecord(
                source_id=source.id,
                session_id=session.id,
                event_start_time=datetime(2026, 3, 14, 9, 0, 0),
                description="成员出现",
                event_type="member_appear",
                title="成员出现",
                summary="爸爸在客厅出现",
                importance_level="medium",
            ),
            EventRecord(
                source_id=source.id,
                session_id=session.id,
                event_start_time=datetime(2026, 3, 14, 10, 0, 0),
                description="未知人员出现",
                event_type="unknown_person_appear",
                title="未知人员出现",
                summary="门口出现未知人员",
                importance_level="high",
            ),
            EventRecord(
                source_id=source.id,
                session_id=session.id,
                event_start_time=datetime(2026, 3, 14, 11, 0, 0),
                description="宠物停留",
                event_type="pet_stay",
                title="宠物停留",
                summary="布丁在阳台停留",
                importance_level="low",
            ),
        ]
    )
    pg_db.commit()

    response = get_daily_summary(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        date_str="2026-03-14",
    )

    assert response.code == 0
    assert response.data is not None
    assert response.data.detail_text is not None
    assert "对象小结" in response.data.detail_text
    assert "关注事项" in response.data.detail_text


def test_generate_all_daily_summaries_dispatches_full_date_range(pg_db: Session) -> None:
    _, s10 = _seed_source_and_session(
        pg_db,
        session_start=datetime(2026, 3, 10, 8, 0, 0),
        session_end=datetime(2026, 3, 10, 8, 30, 0),
    )
    _, s12 = _seed_source_and_session(
        pg_db,
        session_start=datetime(2026, 3, 12, 9, 0, 0),
        session_end=datetime(2026, 3, 12, 9, 20, 0),
    )
    pg_db.commit()

    dispatched_dates: list[str] = []
    mock_orchestrator = MagicMock()

    def _mock_dispatch(_db, command):
        dispatched_dates.append(str(command.target_date_str))
        return f"task-{command.target_date_str}"

    mock_orchestrator.dispatch_generate_daily_summary.side_effect = _mock_dispatch

    response = generate_all_daily_summaries(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        orchestrator=mock_orchestrator,
    )

    assert response.code == 0
    assert response.data["earliest_date"] == "2026-03-10"
    assert response.data["latest_date"] == "2026-03-12"
    assert response.data["target_dates"] == ["2026-03-10", "2026-03-12"]
    assert response.data["queued_count"] == 2
    assert dispatched_dates == ["2026-03-10", "2026-03-12"]


def test_generate_all_daily_summaries_skips_active_dates(pg_db: Session) -> None:
    _, _ = _seed_source_and_session(
        pg_db,
        session_start=datetime(2026, 3, 10, 8, 0, 0),
        session_end=datetime(2026, 3, 10, 8, 30, 0),
    )
    _, _ = _seed_source_and_session(
        pg_db,
        session_start=datetime(2026, 3, 12, 9, 0, 0),
        session_end=datetime(2026, 3, 12, 9, 20, 0),
    )
    pg_db.add(
        TaskLog(
            task_type=TaskType.DAILY_SUMMARY_GENERATION,
            status=TaskStatus.RUNNING,
            detail_json={
                "target_date": "2026-03-10",
                "dedupe_key": "daily_summary_generation|2026-03-10",
            },
            dedupe_key="daily_summary_generation|2026-03-10",
        )
    )
    pg_db.commit()

    mock_orchestrator = MagicMock()
    mock_orchestrator.dispatch_generate_daily_summary.side_effect = lambda _db, command: (
        f"task-{command.target_date_str}"
    )

    response = generate_all_daily_summaries(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        orchestrator=mock_orchestrator,
    )

    assert response.code == 0
    assert response.data["queued_count"] == 1
    assert response.data["skipped_count"] == 1
    assert response.data["target_dates"] == ["2026-03-10", "2026-03-12"]
    assert response.data["skipped_dates"] == ["2026-03-10"]
