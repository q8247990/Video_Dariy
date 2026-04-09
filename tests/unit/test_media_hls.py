from datetime import datetime
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.v1.endpoints.media import get_session_playback, stream_session_hls_manifest
from src.core.config import settings
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSession.__table__.create(bind=engine)
    VideoFile.__table__.create(bind=engine)
    VideoSessionFileRel.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def test_session_playback_returns_stream_and_manifest() -> None:
    db = _new_db_session()
    with TemporaryDirectory() as tmp_dir:
        old_cache_root = settings.PLAYBACK_CACHE_ROOT
        settings.PLAYBACK_CACHE_ROOT = tmp_dir
        try:
            session = VideoSession(
                source_id=1,
                session_start_time=datetime(2026, 3, 14, 8, 0, 0),
                session_end_time=datetime(2026, 3, 14, 8, 2, 0),
                total_duration_seconds=120,
                analysis_status="pending",
            )
            db.add(session)
            db.flush()

            file_a = VideoFile(
                source_id=1,
                file_name="0800.mp4",
                file_path="/tmp/0800.mp4",
                storage_type="local_file",
                file_format="mp4",
                start_time=datetime(2026, 3, 14, 8, 0, 0),
                end_time=datetime(2026, 3, 14, 8, 1, 0),
                duration_seconds=60,
                parse_status="parsed",
            )
            file_b = VideoFile(
                source_id=1,
                file_name="0801.mp4",
                file_path="/tmp/0801.mp4",
                storage_type="local_file",
                file_format="mp4",
                start_time=datetime(2026, 3, 14, 8, 1, 0),
                end_time=datetime(2026, 3, 14, 8, 2, 0),
                duration_seconds=60,
                parse_status="parsed",
            )
            db.add(file_a)
            db.add(file_b)
            db.flush()

            db.add(
                VideoSessionFileRel(session_id=session.id, video_file_id=file_a.id, sort_index=0)
            )
            db.add(
                VideoSessionFileRel(session_id=session.id, video_file_id=file_b.id, sort_index=1)
            )
            db.commit()

            playback_resp = get_session_playback(session_id=session.id, db=db, locale="zh-CN")
            assert playback_resp.code == 0
            assert playback_resp.data is not None
            assert playback_resp.data["playback_url"] == f"/media/sessions/{session.id}/stream"
            assert playback_resp.data["hls_url"] == f"/media/sessions/{session.id}/hls/index.m3u8"

            manifest_resp = stream_session_hls_manifest(
                session_id=session.id, db=db, locale="zh-CN"
            )
            manifest_text = manifest_resp.body.decode("utf-8")
            assert "#EXTM3U" in manifest_text
            assert f"{settings.API_V1_STR}/media/files/{file_a.id}/stream" in manifest_text
            assert f"{settings.API_V1_STR}/media/files/{file_b.id}/stream" in manifest_text
        finally:
            settings.PLAYBACK_CACHE_ROOT = old_cache_root
            db.close()
