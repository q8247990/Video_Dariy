from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.services.video_source_status import (
    build_video_source_status,
    build_video_sources_status_map,
)


def test_build_video_source_status_core_metrics(pg_db: Session) -> None:
    source = VideoSource(
        source_name="客厅",
        camera_name="Cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/data/videos/cam1"},
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()

    pg_db.add_all(
        [
            VideoFile(
                source_id=source.id,
                file_name="f1.mp4",
                file_path="/data/videos/cam1/f1.mp4",
                start_time=datetime(2026, 3, 10, 8, 0, 0),
                end_time=datetime(2026, 3, 10, 8, 30, 0),
                duration_seconds=1800,
            ),
            VideoFile(
                source_id=source.id,
                file_name="f2.mp4",
                file_path="/data/videos/cam1/f2.mp4",
                start_time=datetime(2026, 3, 10, 8, 30, 0),
                end_time=datetime(2026, 3, 10, 9, 0, 0),
                duration_seconds=1800,
            ),
        ]
    )

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 10, 8, 0, 0),
        session_end_time=datetime(2026, 3, 10, 8, 30, 0),
        total_duration_seconds=1800,
        analysis_status=SessionAnalysisStatus.SUCCESS,
    )
    pg_db.add(session)
    pg_db.flush()
    first_file = (
        pg_db.query(VideoFile)
        .filter(VideoFile.source_id == source.id, VideoFile.file_name == "f1.mp4")
        .first()
    )
    assert first_file is not None
    pg_db.add(
        VideoSessionFileRel(session_id=session.id, video_file_id=first_file.id, sort_index=0)
    )
    pg_db.commit()

    result = build_video_source_status(db=pg_db, source_id=source.id)
    # PG ``timestamptz`` round-trips a tz-aware datetime (Asia/Shanghai
    # in this environment); strip the tz before equality comparison.
    def _naive(value: datetime) -> datetime:
        return value.replace(tzinfo=None) if value.tzinfo else value

    assert _naive(result["video_earliest_time"]) == datetime(2026, 3, 10, 8, 0, 0)
    assert _naive(result["video_latest_time"]) == datetime(2026, 3, 10, 9, 0, 0)
    assert _naive(result["analyzed_earliest_time"]) == datetime(2026, 3, 10, 8, 0, 0)
    assert _naive(result["analyzed_latest_time"]) == datetime(2026, 3, 10, 8, 30, 0)
    assert result["analyzed_coverage_percent"] == 50.0
    assert result["analysis_state"] == "stopped"
    assert result["minutes_since_last_new_video"] is not None


def test_build_video_source_status_paused_has_highest_priority(pg_db: Session) -> None:
    source = VideoSource(
        source_name="门口",
        camera_name="Cam2",
        location_name="门口",
        source_type="local_directory",
        config_json={"root_path": "/data/videos/cam2"},
        enabled=True,
        source_paused=True,
        paused_at=datetime.utcnow(),
    )
    pg_db.add(source)
    pg_db.flush()

    pg_db.add(
        TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=source.id,
            status=TaskStatus.RUNNING,
        )
    )
    pg_db.commit()

    result = build_video_source_status(db=pg_db, source_id=source.id)
    assert result["analysis_state"] == "paused"


def test_build_video_source_status_analyzing_from_tasks_or_sessions(pg_db: Session) -> None:
    source = VideoSource(
        source_name="阳台",
        camera_name="Cam3",
        location_name="阳台",
        source_type="local_directory",
        config_json={"root_path": "/data/videos/cam3"},
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()

    pg_db.add(
        TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=source.id,
            status=TaskStatus.RUNNING,
        )
    )
    pg_db.commit()

    running_result = build_video_source_status(db=pg_db, source_id=source.id)
    assert running_result["analysis_state"] == "analyzing"

    pg_db.query(TaskLog).update({"status": TaskStatus.SUCCESS})
    pg_db.add(
        VideoSession(
            source_id=source.id,
            session_start_time=datetime.utcnow() - timedelta(minutes=5),
            session_end_time=datetime.utcnow() - timedelta(minutes=1),
            total_duration_seconds=240,
            analysis_status=SessionAnalysisStatus.ANALYZING,
        )
    )
    pg_db.commit()

    session_result = build_video_source_status(db=pg_db, source_id=source.id)
    assert session_result["analysis_state"] == "analyzing"


def test_build_video_sources_status_map_for_ten_sources(pg_db: Session) -> None:
    source_ids: list[int] = []
    for idx in range(10):
        source = VideoSource(
            source_name=f"源{idx}",
            camera_name=f"Cam{idx}",
            location_name=f"位置{idx}",
            source_type="local_directory",
            config_json={"root_path": f"/data/videos/cam{idx}"},
            enabled=True,
        )
        pg_db.add(source)
        pg_db.flush()
        source_ids.append(source.id)
    pg_db.commit()

    result = build_video_sources_status_map(db=pg_db, source_ids=source_ids)
    assert len(result) == 10
    assert set(result.keys()) == set(source_ids)
