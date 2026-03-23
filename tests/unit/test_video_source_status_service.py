from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.system_config import SystemConfig
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.models.video_source_runtime_state import VideoSourceRuntimeState
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.services.video_source_status import (
    build_video_source_status,
    build_video_sources_status_map,
)


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    VideoFile.__table__.create(bind=engine)
    VideoSession.__table__.create(bind=engine)
    VideoSessionFileRel.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    SystemConfig.__table__.create(bind=engine)
    VideoSourceRuntimeState.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def test_build_video_source_status_core_metrics() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="客厅",
            camera_name="Cam1",
            location_name="客厅",
            source_type="local_directory",
            config_json={"root_path": "/data/videos/cam1"},
            enabled=True,
        )
        db.add(source)
        db.flush()

        db.add_all(
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
        db.add(session)
        db.flush()
        first_file = (
            db.query(VideoFile)
            .filter(VideoFile.source_id == source.id, VideoFile.file_name == "f1.mp4")
            .first()
        )
        assert first_file is not None
        db.add(
            VideoSessionFileRel(session_id=session.id, video_file_id=first_file.id, sort_index=0)
        )
        db.commit()

        result = build_video_source_status(db=db, source_id=source.id)
        assert result["video_earliest_time"] == datetime(2026, 3, 10, 8, 0, 0)
        assert result["video_latest_time"] == datetime(2026, 3, 10, 9, 0, 0)
        assert result["analyzed_earliest_time"] == datetime(2026, 3, 10, 8, 0, 0)
        assert result["analyzed_latest_time"] == datetime(2026, 3, 10, 8, 30, 0)
        assert result["analyzed_coverage_percent"] == 50.0
        assert result["analysis_state"] == "stopped"
        assert result["minutes_since_last_new_video"] is not None
    finally:
        db.close()


def test_build_video_source_status_paused_has_highest_priority() -> None:
    db = _new_db_session()
    try:
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
        db.add(source)
        db.flush()

        db.add(
            TaskLog(
                task_type=TaskType.SESSION_BUILD,
                task_target_id=source.id,
                status=TaskStatus.RUNNING,
            )
        )
        db.commit()

        result = build_video_source_status(db=db, source_id=source.id)
        assert result["analysis_state"] == "paused"
    finally:
        db.close()


def test_build_video_source_status_analyzing_from_tasks_or_sessions() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="阳台",
            camera_name="Cam3",
            location_name="阳台",
            source_type="local_directory",
            config_json={"root_path": "/data/videos/cam3"},
            enabled=True,
        )
        db.add(source)
        db.flush()

        db.add(
            TaskLog(
                task_type=TaskType.SESSION_BUILD,
                task_target_id=source.id,
                status=TaskStatus.RUNNING,
            )
        )
        db.commit()

        running_result = build_video_source_status(db=db, source_id=source.id)
        assert running_result["analysis_state"] == "analyzing"

        db.query(TaskLog).update({"status": TaskStatus.SUCCESS})
        db.add(
            VideoSession(
                source_id=source.id,
                session_start_time=datetime.utcnow() - timedelta(minutes=5),
                session_end_time=datetime.utcnow() - timedelta(minutes=1),
                total_duration_seconds=240,
                analysis_status=SessionAnalysisStatus.ANALYZING,
            )
        )
        db.commit()

        session_result = build_video_source_status(db=db, source_id=source.id)
        assert session_result["analysis_state"] == "analyzing"
    finally:
        db.close()


def test_build_video_sources_status_map_for_ten_sources() -> None:
    db = _new_db_session()
    try:
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
            db.add(source)
            db.flush()
            source_ids.append(source.id)
        db.commit()

        result = build_video_sources_status_map(db=db, source_ids=source_ids)
        assert len(result) == 10
        assert set(result.keys()) == set(source_ids)
    finally:
        db.close()
