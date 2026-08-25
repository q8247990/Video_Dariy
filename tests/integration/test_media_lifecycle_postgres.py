from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import settings
from src.models.event_record import EventRecord
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.session_playback import get_or_create_session_hls_manifest
from src.services.source_deletion import collect_source_deletion_orphan_report

pytestmark = pytest.mark.postgres


def _seed_source(db: Session) -> VideoSource:
    source = VideoSource(
        source_name="media-test",
        camera_name="media-test",
        location_name="test",
        source_type="local_directory",
    )
    db.add(source)
    db.flush()
    return source


def test_concurrent_manifest_writes_are_atomic(postgres_migrated_engine: Engine) -> None:
    with TemporaryDirectory() as cache_root, TemporaryDirectory() as video_root:
        old_cache_root = settings.PLAYBACK_CACHE_ROOT
        old_video_root = settings.VIDEO_ROOT_PATH
        settings.PLAYBACK_CACHE_ROOT = cache_root
        settings.VIDEO_ROOT_PATH = video_root
        try:
            with Session(postgres_migrated_engine) as db:
                source = _seed_source(db)
                start = datetime.now(timezone.utc)
                session = VideoSession(
                    source_id=source.id,
                    session_start_time=start,
                    session_end_time=start + timedelta(minutes=1),
                    analysis_status="sealed",
                )
                db.add(session)
                db.flush()
                path = Path(video_root) / "segment.mp4"
                path.touch()
                video = VideoFile(
                    source_id=source.id,
                    file_name="segment.mp4",
                    file_path=str(path),
                    storage_type="local_file",
                    start_time=start,
                    end_time=start + timedelta(minutes=1),
                    parse_status="parsed",
                )
                db.add(video)
                db.flush()
                db.add(
                    VideoSessionFileRel(session_id=session.id, video_file_id=video.id, sort_index=0)
                )
                db.commit()
                session_id = session.id

            factory = sessionmaker(bind=postgres_migrated_engine)
            with ThreadPoolExecutor(max_workers=4) as executor:
                paths = list(
                    executor.map(
                        lambda _: _write_manifest(factory, session_id),
                        range(4),
                    )
                )

            assert all(path == paths[0] for path in paths)
            payload = Path(paths[0]).read_text(encoding="utf-8")
            assert payload.startswith("#EXTM3U\n")
            assert payload.endswith("#EXT-X-ENDLIST\n")
        finally:
            settings.PLAYBACK_CACHE_ROOT = old_cache_root
            settings.VIDEO_ROOT_PATH = old_video_root


def _write_manifest(factory: sessionmaker[Session], session_id: int) -> str:
    with factory() as db:
        return str(get_or_create_session_hls_manifest(db, session_id).manifest_path)


def test_source_delete_restricts_history_and_preflight_is_clean(
    postgres_migrated_engine: Engine,
) -> None:
    with Session(postgres_migrated_engine) as db:
        source = _seed_source(db)
        start = datetime.now(timezone.utc)
        session = VideoSession(
            source_id=source.id,
            session_start_time=start,
            session_end_time=start + timedelta(minutes=1),
            analysis_status="success",
        )
        db.add(session)
        db.flush()
        db.add(
            EventRecord(
                source_id=source.id,
                session_id=session.id,
                event_start_time=start,
                description="history",
            )
        )
        db.commit()

        assert all(report.count == 0 for report in collect_source_deletion_orphan_report(db))
        with pytest.raises(IntegrityError):
            db.execute(
                text("DELETE FROM video_source WHERE id = :source_id"),
                {"source_id": source.id},
            )
            db.commit()
        db.rollback()
