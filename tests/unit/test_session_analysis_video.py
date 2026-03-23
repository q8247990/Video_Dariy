from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.services.session_analysis_video import (
    _concat_video_files_to_mp4_bytes,
    build_session_video_chunks,
)


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSession.__table__.create(bind=engine)
    VideoFile.__table__.create(bind=engine)
    VideoSessionFileRel.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def test_build_session_video_chunks_split_by_10_minutes() -> None:
    db = _new_db_session()
    try:
        session = VideoSession(
            source_id=1,
            session_start_time=datetime(2026, 3, 14, 9, 0, 0),
            session_end_time=datetime(2026, 3, 14, 9, 25, 0),
            total_duration_seconds=1500,
            analysis_status="pending",
        )
        db.add(session)
        db.flush()

        base = datetime(2026, 3, 14, 9, 0, 0)
        for index in range(25):
            start_time = base + timedelta(minutes=index)
            end_time = start_time + timedelta(minutes=1)
            vf = VideoFile(
                source_id=1,
                file_name=f"{index:04d}.mp4",
                file_path=f"/tmp/{index:04d}.mp4",
                storage_type="local_file",
                file_format="mp4",
                start_time=start_time,
                end_time=end_time,
                duration_seconds=60,
                parse_status="parsed",
            )
            db.add(vf)
            db.flush()
            db.add(
                VideoSessionFileRel(
                    session_id=session.id,
                    video_file_id=vf.id,
                    sort_index=index,
                )
            )
        db.commit()

        chunks = build_session_video_chunks(db, session.id, chunk_seconds=600)

        assert len(chunks) == 3
        assert chunks[0].start_offset_seconds == 0
        assert chunks[0].duration_seconds == 600
        assert len(chunks[0].file_paths) == 10

        assert chunks[1].start_offset_seconds == 600
        assert chunks[1].duration_seconds == 600
        assert len(chunks[1].file_paths) == 10

        assert chunks[2].start_offset_seconds == 1200
        assert chunks[2].duration_seconds == 300
        assert len(chunks[2].file_paths) == 5
    finally:
        db.close()


def test_concat_video_uses_temp_concat_file_input(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _mock_run(cmd, input=None, capture_output=False):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr("src.services.session_analysis_video.subprocess.run", _mock_run)

    result = _concat_video_files_to_mp4_bytes(["/tmp/a.mp4", "/tmp/b.mp4"])

    assert result == b"ok"
    assert len(calls) == 1
    assert "-f" in calls[0]
    assert "concat" in calls[0]
    index = calls[0].index("-i")
    assert calls[0][index + 1].endswith(".txt")
