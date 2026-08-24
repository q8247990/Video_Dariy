from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.services import session_analysis_video as sav
from src.services.session_analysis_video import (
    ChunkKeyframePayload,
    SessionVideoChunk,
    SubChunk,
    _concat_video_files_to_mp4_bytes,
    build_chunk_keyframe_payload,
    build_chunk_sub_chunks,
    build_session_video_chunks,
    session_chunk_from_sub_chunk,
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


def test_build_session_video_chunks_populates_file_durations() -> None:
    db = _new_db_session()
    try:
        session = VideoSession(
            source_id=1,
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

        assert len(chunks) == 1
        assert chunks[0].file_durations == [60, 60]
    finally:
        db.close()


def test_build_chunk_keyframe_payload_uses_ascending_indices_and_jpeg_url(
    monkeypatch: object,
) -> None:
    del monkeypatch

    class _FakeKS:
        jpeg_base64_list = ["AAA", "BBB"]
        fps = 19.955
        total_num_frames = 5985
        frames_indices = [24, 130]
        extra = {"frames_decoded": 100}

    def _fake_extract(file_paths: list[str], **kwargs):  # type: ignore[no-untyped-def]
        del file_paths, kwargs
        return _FakeKS()

    sav.extract_keyframes_for_sub_chunk = _fake_extract  # type: ignore[assignment]

    sub_chunk = SubChunk(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=300,
        file_paths=["/fake/a.mp4"],
    )

    payload = build_chunk_keyframe_payload(sub_chunk)

    assert isinstance(payload, ChunkKeyframePayload)
    assert payload.jpeg_data_url == "data:video/jpeg;base64,AAA,BBB"
    video_meta = payload.media_io_kwargs["video"]
    assert video_meta["fps"] == 19.955
    assert video_meta["total_num_frames"] == 5985
    assert video_meta["frames_indices"] == [24, 130]
    assert video_meta["num_frames"] == -1
    assert payload.diagnostics == {"frames_decoded": 100}


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
