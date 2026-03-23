from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.v1.endpoints.video_sources import (
    delete_video_source,
    get_video_sources_status_batch,
    update_video_source,
)
from src.models.system_config import SystemConfig
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.models.video_source_runtime_state import VideoSourceRuntimeState
from src.schemas.video_source import VideoSourceUpdate
from src.services.pipeline_constants import TaskStatus, TaskType


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    VideoSourceRuntimeState.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    SystemConfig.__table__.create(bind=engine)
    VideoFile.__table__.create(bind=engine)
    VideoSession.__table__.create(bind=engine)
    VideoSessionFileRel.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def _current_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


def test_update_video_source_resets_validation_when_config_changed() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="客厅",
            camera_name="cam1",
            location_name="客厅",
            source_type="local_directory",
            config_json={"root_path": "/tmp/a"},
            enabled=True,
            last_validate_status="success",
            last_validate_message="ok",
            last_validate_at=datetime(2026, 3, 10, 10, 0, 0),
        )
        db.add(source)
        db.commit()

        resp = update_video_source(
            db=db,
            current_user=_current_user(),
            id=source.id,
            data=VideoSourceUpdate(config_json={"root_path": "/tmp/b"}),
        )
        assert resp.code == 0
        assert resp.data.last_validate_status is None
        assert resp.data.last_validate_message is None
        assert resp.data.last_validate_at is None
    finally:
        db.close()


def test_delete_video_source_blocks_running_task() -> None:
    db = _new_db_session()
    try:
        source = VideoSource(
            source_name="门口",
            camera_name="cam2",
            location_name="门口",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
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

        resp = delete_video_source(db=db, current_user=_current_user(), id=source.id)
        assert resp.code == 4004
        assert "running task" in str(resp.message)
    finally:
        db.close()


def test_video_source_status_batch_returns_multiple_sources() -> None:
    db = _new_db_session()
    try:
        s1 = VideoSource(
            source_name="A",
            camera_name="camA",
            location_name="A",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
        )
        s2 = VideoSource(
            source_name="B",
            camera_name="camB",
            location_name="B",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
        )
        db.add_all([s1, s2])
        db.commit()

        resp = get_video_sources_status_batch(
            db=db,
            current_user=_current_user(),
            source_ids=f"{s1.id},{s2.id}",
        )
        assert resp.code == 0
        assert len(resp.data) == 2
        assert {item.source_id for item in resp.data} == {s1.id, s2.id}
    finally:
        db.close()
