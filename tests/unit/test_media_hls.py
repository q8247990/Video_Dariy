from datetime import datetime
from pathlib import Path
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
    with TemporaryDirectory() as tmp_dir, TemporaryDirectory() as video_root:
        old_cache_root = settings.PLAYBACK_CACHE_ROOT
        old_video_root = settings.VIDEO_ROOT_PATH
        settings.PLAYBACK_CACHE_ROOT = tmp_dir
        settings.VIDEO_ROOT_PATH = video_root
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
                file_path=str(Path(video_root) / "0800.mp4"),
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
                file_path=str(Path(video_root) / "0801.mp4"),
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
            Path(file_a.file_path).touch()
            Path(file_b.file_path).touch()

            db.add(
                VideoSessionFileRel(session_id=session.id, video_file_id=file_a.id, sort_index=0)
            )
            db.add(
                VideoSessionFileRel(session_id=session.id, video_file_id=file_b.id, sort_index=1)
            )
            db.commit()

            playback_resp = get_session_playback(
                session_id=session.id, db=db, locale="zh-CN", current_user=None
            )
            assert playback_resp.code == 0
            assert playback_resp.data is not None
            assert playback_resp.data["playback_url"].startswith(
                f"/media/sessions/{session.id}/stream?token="
            )
            assert playback_resp.data["hls_url"].startswith(
                f"/media/sessions/{session.id}/hls/index.m3u8?token="
            )

            manifest_resp = stream_session_hls_manifest(
                session_id=session.id,
                db=db,
                locale="zh-CN",
                token=playback_resp.data["hls_url"].split("token=", maxsplit=1)[1],
            )
            manifest_text = manifest_resp.body.decode("utf-8")
            assert "#EXTM3U" in manifest_text
            assert f"{settings.API_V1_STR}/media/files/{file_a.id}/stream?token=" in manifest_text
            assert f"{settings.API_V1_STR}/media/files/{file_b.id}/stream?token=" in manifest_text
            assert manifest_resp.headers["cache-control"] == "no-store"
        finally:
            settings.PLAYBACK_CACHE_ROOT = old_cache_root
            settings.VIDEO_ROOT_PATH = old_video_root
            db.close()


def test_session_playback_marks_missing_segments_and_keeps_available_segment() -> None:
    db = _new_db_session()
    with TemporaryDirectory() as cache_dir, TemporaryDirectory() as video_root:
        old_cache_root = settings.PLAYBACK_CACHE_ROOT
        old_video_root = settings.VIDEO_ROOT_PATH
        settings.PLAYBACK_CACHE_ROOT = cache_dir
        settings.VIDEO_ROOT_PATH = video_root
        try:
            session = VideoSession(
                source_id=1,
                session_start_time=datetime(2026, 3, 14, 8, 0, 0),
                session_end_time=datetime(2026, 3, 14, 8, 2, 0),
                analysis_status="pending",
            )
            db.add(session)
            db.flush()
            available_path = Path(video_root) / "available.mp4"
            available_path.touch()
            available = VideoFile(
                source_id=1,
                file_name="available.mp4",
                file_path=str(available_path),
                storage_type="local_file",
                start_time=datetime(2026, 3, 14, 8, 0, 0),
                end_time=datetime(2026, 3, 14, 8, 1, 0),
                parse_status="parsed",
            )
            missing = VideoFile(
                source_id=1,
                file_name="missing.mp4",
                file_path=str(Path(video_root) / "missing.mp4"),
                storage_type="local_file",
                start_time=datetime(2026, 3, 14, 8, 1, 0),
                end_time=datetime(2026, 3, 14, 8, 2, 0),
                parse_status="parsed",
            )
            db.add_all([available, missing])
            db.flush()
            db.add_all(
                [
                    VideoSessionFileRel(
                        session_id=session.id, video_file_id=available.id, sort_index=0
                    ),
                    VideoSessionFileRel(
                        session_id=session.id, video_file_id=missing.id, sort_index=1
                    ),
                ]
            )
            db.commit()

            playback = get_session_playback(session.id, db, "zh-CN", None)

            assert playback.data["availability"] == "partial"
            assert playback.data["files"][0]["available"] is True
            assert playback.data["files"][1]["available"] is False
            assert playback.data["files"][1]["stream_url"] is None
            assert db.get(VideoFile, missing.id).file_missing is True
            assert db.get(VideoFile, missing.id).missing_at is not None
            manifest = stream_session_hls_manifest(
                session.id,
                db,
                "zh-CN",
                playback.data["hls_url"].split("token=", maxsplit=1)[1],
            )
            assert f"/files/{available.id}/stream?token=" in manifest.body.decode()
            assert f"/files/{missing.id}/stream?token=" not in manifest.body.decode()
            assert not (Path(cache_dir) / f"session_{session.id}" / "meta.json").exists()
        finally:
            settings.PLAYBACK_CACHE_ROOT = old_cache_root
            settings.VIDEO_ROOT_PATH = old_video_root
            db.close()
