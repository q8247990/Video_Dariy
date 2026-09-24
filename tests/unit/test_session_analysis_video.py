"""Unit tests for the file-level session analysis helpers.

Session analysis is now one file = one sub-chunk; these tests pin that
shape (never merging short files, never splitting a long file).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.session_analysis_video import (
    build_file_sub_chunks,
    build_video_data_url,
)


def _seed_session(db: Session, durations: list[int]) -> VideoSession:
    source = VideoSource(
        source_name="chunk-test",
        camera_name="cam",
        location_name="loc",
        source_type="local_directory",
    )
    db.add(source)
    db.flush()
    start = datetime(2026, 3, 14, 9, 0, 0)
    session = VideoSession(
        source_id=source.id,
        session_start_time=start,
        session_end_time=start + timedelta(seconds=sum(durations)),
        total_duration_seconds=sum(durations),
        analysis_status="pending",
    )
    db.add(session)
    db.flush()

    base = start
    for index, duration in enumerate(durations):
        video_file = VideoFile(
            source_id=source.id,
            file_name=f"{index:04d}.mp4",
            file_path=f"/tmp/{index:04d}.mp4",
            storage_type="local_file",
            file_format="mp4",
            start_time=base,
            end_time=base + timedelta(seconds=duration),
            duration_seconds=duration,
            parse_status="parsed",
        )
        db.add(video_file)
        db.flush()
        db.add(
            VideoSessionFileRel(
                session_id=session.id,
                video_file_id=video_file.id,
                sort_index=index,
            )
        )
        base = base + timedelta(seconds=duration)
    db.commit()
    return session


def test_build_file_sub_chunks_one_per_file(pg_db: Session) -> None:
    session = _seed_session(pg_db, [60, 60, 60])

    sub_chunks = build_file_sub_chunks(pg_db, session.id)

    assert [sub.sub_chunk_index for sub in sub_chunks] == [0, 1, 2]
    assert [sub.chunk_index for sub in sub_chunks] == [0, 0, 0]
    assert [sub.start_offset_seconds for sub in sub_chunks] == [0, 60, 120]
    assert [sub.duration_seconds for sub in sub_chunks] == [60, 60, 60]
    assert all(len(sub.file_paths) == 1 for sub in sub_chunks)


def test_build_file_sub_chunks_never_merges_short_files(pg_db: Session) -> None:
    session = _seed_session(pg_db, [58, 2, 20])

    sub_chunks = build_file_sub_chunks(pg_db, session.id)

    assert len(sub_chunks) == 3
    assert all(len(sub.file_paths) == 1 for sub in sub_chunks)
    assert [sub.duration_seconds for sub in sub_chunks] == [58, 2, 20]
    assert [sub.start_offset_seconds for sub in sub_chunks] == [0, 58, 60]


def test_build_video_data_url_wraps_single_file(tmp_path) -> None:
    mp4_path = tmp_path / "fake.mp4"
    mp4_path.write_bytes(b"\x00\x01\x02\x03")

    url = build_video_data_url(str(mp4_path))

    assert url.startswith("data:video/mp4;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x00\x01\x02\x03"
