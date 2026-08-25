from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.v1.endpoints.media import get_session_playback
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel


def test_get_session_playback_loads_files_in_one_batch(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    VideoSession.__table__.create(bind=engine)
    VideoFile.__table__.create(bind=engine)
    VideoSessionFileRel.__table__.create(bind=engine)
    db: Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    monkeypatch.setattr("src.api.v1.endpoints.media.is_video_file_available", lambda _: True)
    statements: list[str] = []

    def _capture_sql(_, __, statement, ___, ____, _____) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture_sql)
    try:
        session = VideoSession(
            source_id=1,
            session_start_time=datetime(2026, 3, 14, 8, 0),
            session_end_time=datetime(2026, 3, 14, 8, 3),
            total_duration_seconds=180,
            analysis_status="sealed",
        )
        db.add(session)
        db.flush()
        session_id = session.id
        files = [
            VideoFile(
                source_id=1,
                file_name=f"clip-{index}.mp4",
                file_path=f"/videos/clip-{index}.mp4",
                start_time=session.session_start_time,
                end_time=session.session_end_time,
                duration_seconds=60,
            )
            for index in range(3)
        ]
        db.add_all(files)
        db.flush()
        db.add_all(
            VideoSessionFileRel(session_id=session_id, video_file_id=file.id, sort_index=index)
            for index, file in enumerate(files)
        )
        db.commit()
        statements.clear()

        response = get_session_playback(session_id, db, "zh-CN", object())

        assert [file["file_id"] for file in response.data["files"]] == [file.id for file in files]
        assert sum("FROM video_file" in statement for statement in statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", _capture_sql)
        db.close()
