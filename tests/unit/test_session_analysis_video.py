from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.session_analysis_video import (
    SessionVideoChunk,
    SubChunk,
    _concat_video_files_to_mp4_bytes,
    build_chunk_sub_chunks,
    build_chunk_video_data_url,
    build_session_video_chunks,
    session_chunk_from_sub_chunk,
)


@pytest.fixture()
def db_session(pg_db: Session) -> Session:
    """Single session against the project-wide ``pg_db`` fixture (PostgreSQL)."""
    return pg_db


def test_build_session_video_chunks_split_by_10_minutes(db_session: Session) -> None:
    db = db_session
    source = VideoSource(
        source_name="chunk-test",
        camera_name="cam",
        location_name="loc",
        source_type="local_directory",
    )
    db.add(source)
    db.flush()
    session = VideoSession(
        source_id=source.id,
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
            source_id=source.id,
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


def test_concat_video_delegates_to_run_ffmpeg_concat(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _mock_concat(paths: list[str]) -> bytes:
        calls.append(paths)
        return b"ok"

    monkeypatch.setattr(
        "src.services.session_analysis_video.run_ffmpeg_concat_to_bytes", _mock_concat
    )

    result = _concat_video_files_to_mp4_bytes(["/tmp/a.mp4", "/tmp/b.mp4"])

    assert result == b"ok"
    assert len(calls) == 1
    assert calls[0] == ["/tmp/a.mp4", "/tmp/b.mp4"]


def test_build_chunk_sub_chunks_splits_into_300s() -> None:
    chunk = SessionVideoChunk(
        chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=600,
        file_paths=[f"/fake/f{i}.mp4" for i in range(10)],
        file_durations=[60] * 10,
    )

    sub_chunks = build_chunk_sub_chunks(chunk, db=None, sub_chunk_seconds=300)

    assert len(sub_chunks) == 2
    assert sub_chunks[0].chunk_index == 0
    assert sub_chunks[0].sub_chunk_index == 0
    assert sub_chunks[0].start_offset_seconds == 0
    assert sub_chunks[0].duration_seconds == 300
    assert len(sub_chunks[0].file_paths) == 5

    assert sub_chunks[1].sub_chunk_index == 1
    assert sub_chunks[1].start_offset_seconds == 300
    assert sub_chunks[1].duration_seconds == 300
    assert len(sub_chunks[1].file_paths) == 5


def test_build_chunk_sub_chunks_does_not_split_short_parent() -> None:
    chunk = SessionVideoChunk(
        chunk_index=2,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=["/fake/short.mp4"],
        file_durations=[60],
    )

    sub_chunks = build_chunk_sub_chunks(chunk, db=None, sub_chunk_seconds=300)

    assert len(sub_chunks) == 1
    assert sub_chunks[0].chunk_index == 2
    assert sub_chunks[0].sub_chunk_index == 0
    assert sub_chunks[0].duration_seconds == 60


def test_build_chunk_sub_chunks_preserves_absolute_offset() -> None:
    chunk = SessionVideoChunk(
        chunk_index=1,
        start_offset_seconds=600,
        duration_seconds=600,
        file_paths=[f"/fake/f{i}.mp4" for i in range(10)],
        file_durations=[60] * 10,
    )

    sub_chunks = build_chunk_sub_chunks(chunk, db=None, sub_chunk_seconds=300)

    assert sub_chunks[0].start_offset_seconds == 600
    assert sub_chunks[1].start_offset_seconds == 900


def test_build_chunk_sub_chunks_falls_back_to_60s_when_durations_missing() -> None:
    chunk = SessionVideoChunk(
        chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=600,
        file_paths=[f"/fake/f{i}.mp4" for i in range(10)],
        file_durations=None,
    )

    sub_chunks = build_chunk_sub_chunks(chunk, db=None, sub_chunk_seconds=300)

    assert len(sub_chunks) == 2
    assert sub_chunks[0].duration_seconds == 300
    assert sub_chunks[1].duration_seconds == 300


def test_build_session_video_chunks_populates_file_durations(db_session: Session) -> None:
    db = db_session
    source = VideoSource(
        source_name="chunk-test",
        camera_name="cam",
        location_name="loc",
        source_type="local_directory",
    )
    db.add(source)
    db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 14, 9, 0, 0),
        session_end_time=datetime(2026, 3, 14, 9, 2, 0),
        total_duration_seconds=120,
        analysis_status="pending",
    )
    db.add(session)
    db.flush()

    base = datetime(2026, 3, 14, 9, 0, 0)
    for index in range(2):
        start_time = base + timedelta(minutes=index)
        end_time = start_time + timedelta(minutes=1)
        vf = VideoFile(
            source_id=source.id,
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

    assert len(chunks) == 1
    assert chunks[0].file_durations == [60, 60]


def test_session_chunk_from_sub_chunk_projects_correctly() -> None:
    sub_chunk = SubChunk(
        chunk_index=3,
        sub_chunk_index=1,
        start_offset_seconds=900,
        duration_seconds=300,
        file_paths=["/fake/a.mp4", "/fake/b.mp4"],
    )
    projected = session_chunk_from_sub_chunk(sub_chunk, parent_chunk_index=3)
    assert projected.chunk_index == 3
    assert projected.start_offset_seconds == 900
    assert projected.duration_seconds == 300
    assert projected.file_paths == ["/fake/a.mp4", "/fake/b.mp4"]


def test_build_chunk_video_data_url_wraps_mp4_data_url(tmp_path) -> None:
    mp4_path = tmp_path / "fake.mp4"
    mp4_path.write_bytes(b"\x00\x01\x02\x03")
    chunk = SessionVideoChunk(
        chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=[str(mp4_path)],
    )
    url = build_chunk_video_data_url(chunk)
    assert url.startswith("data:video/mp4;base64,")
    import base64 as _b64

    raw = _b64.b64decode(url.split(",", 1)[1])
    assert raw == b"\x00\x01\x02\x03"
