from datetime import datetime

from sqlalchemy.orm import Session

from src.api.v1.endpoints.media import get_session_playback
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource


def test_get_session_playback_loads_files_in_one_batch(
    monkeypatch, pg_db: Session
) -> None:
    monkeypatch.setattr("src.api.v1.endpoints.media.is_video_file_available", lambda _: True)

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
        session_start_time=datetime(2026, 3, 14, 8, 0),
        session_end_time=datetime(2026, 3, 14, 8, 3),
        total_duration_seconds=180,
        analysis_status="sealed",
    )
    pg_db.add(session)
    pg_db.flush()
    session_id = session.id
    files = [
        VideoFile(
            source_id=source.id,
            file_name=f"clip-{index}.mp4",
            file_path=f"/videos/clip-{index}.mp4",
            start_time=session.session_start_time,
            end_time=session.session_end_time,
            duration_seconds=60,
        )
        for index in range(3)
    ]
    pg_db.add_all(files)
    pg_db.flush()
    pg_db.add_all(
        VideoSessionFileRel(session_id=session_id, video_file_id=file.id, sort_index=index)
        for index, file in enumerate(files)
    )
    pg_db.commit()

    response = get_session_playback(session_id, pg_db, "zh-CN", object())

    assert [file["file_id"] for file in response.data["files"]] == [file.id for file in files]
    assert all(file_data["available"] is True for file_data in response.data["files"])
