from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from src.api.v1.endpoints.video_sources import (
    delete_video_source,
    get_video_sources_status_batch,
    update_video_source,
)
from src.models.event_record import EventRecord
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.schemas.video_source import VideoSourceUpdate
from src.services.pipeline_constants import TaskStatus, TaskType


def _current_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


def test_update_video_source_resets_validation_when_config_changed(pg_db: Session) -> None:
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
    pg_db.add(source)
    pg_db.commit()

    resp = update_video_source(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        id=source.id,
        data=VideoSourceUpdate(config_json={"root_path": "/tmp/b"}),
    )
    assert resp.code == 0
    assert resp.data.last_validate_status is None
    assert resp.data.last_validate_message is None
    assert resp.data.last_validate_at is None


def test_delete_video_source_blocks_running_task(pg_db: Session) -> None:
    source = VideoSource(
        source_name="门口",
        camera_name="cam2",
        location_name="门口",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
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

    resp = delete_video_source(
        db=pg_db, current_user=_current_user(), locale="zh-CN", id=source.id
    )
    assert resp.code == 4004
    assert "running task" in str(resp.message)


def test_delete_video_source_restricts_retained_event_history(pg_db: Session) -> None:
    source = VideoSource(
        source_name="history",
        camera_name="cam-history",
        location_name="test",
        source_type="local_directory",
    )
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 10, 10, 0, 0),
        session_end_time=datetime(2026, 3, 10, 10, 1, 0),
        analysis_status="success",
    )
    pg_db.add(session)
    pg_db.flush()
    pg_db.add(
        EventRecord(
            source_id=source.id,
            session_id=session.id,
            event_start_time=datetime(2026, 3, 10, 10, 0, 0),
            description="retained history",
        )
    )
    pg_db.commit()

    response = delete_video_source(
        db=pg_db, current_user=_current_user(), locale="zh-CN", id=source.id
    )

    assert response.code == 4004
    assert pg_db.get(VideoSource, source.id) is not None


def test_video_source_status_batch_returns_multiple_sources(pg_db: Session) -> None:
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
    pg_db.add_all([s1, s2])
    pg_db.commit()

    resp = get_video_sources_status_batch(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        source_ids=f"{s1.id},{s2.id}",
    )
    assert resp.code == 0
    assert len(resp.data) == 2
    assert {item.source_id for item in resp.data} == {s1.id, s2.id}
