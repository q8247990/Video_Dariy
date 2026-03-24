from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401
from src.db.base_class import Base
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.services.pipeline_constants import ScanMode, SessionAnalysisStatus
from src.services.session_builder import SessionBuilder


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def _record(start_time: datetime, suffix: str) -> dict:
    end_time = start_time + timedelta(seconds=60)
    return {
        "file_name": f"{suffix}.mp4",
        "file_path": f"/tmp/{suffix}.mp4",
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": 60,
        "file_size": 1024,
        "file_format": "mp4",
        "storage_type": "local_file",
    }


def test_build_keeps_files_with_one_second_gap_in_same_session(monkeypatch) -> None:
    db = _new_db_session()
    try:
        base = datetime(2026, 3, 15, 9, 4, 4)
        records = [
            _record(base, "a"),
            _record(base + timedelta(seconds=61), "b"),
        ]

        monkeypatch.setattr(
            "src.services.session_builder.XiaomiDirectoryParser.scan_directory",
            lambda self, min_time=None, max_time=None, cancel_check=None: records,
        )

        result = SessionBuilder().build(
            db,
            source_id=1,
            root_path="/tmp/videos",
            scan_mode=ScanMode.FULL,
            scan_start=datetime(2026, 3, 15, 0, 0, 0),
            scan_end=datetime(2026, 3, 16, 0, 0, 0),
        )
        db.commit()

        sessions = db.query(VideoSession).order_by(VideoSession.session_start_time.asc()).all()
        rels = db.query(VideoSessionFileRel).order_by(VideoSessionFileRel.sort_index.asc()).all()

        assert result.sessions_created == 1
        assert len(sessions) == 1
        assert len(rels) == 2
        assert sessions[0].session_start_time == base
        assert sessions[0].session_end_time == base + timedelta(seconds=121)
        assert sessions[0].total_duration_seconds == 121
        assert sessions[0].analysis_status == SessionAnalysisStatus.SEALED
    finally:
        db.close()


def test_build_splits_sessions_when_gap_exceeds_one_second(monkeypatch) -> None:
    db = _new_db_session()
    try:
        base = datetime(2026, 3, 15, 9, 4, 4)
        records = [
            _record(base, "a"),
            _record(base + timedelta(seconds=62), "b"),
        ]

        monkeypatch.setattr(
            "src.services.session_builder.XiaomiDirectoryParser.scan_directory",
            lambda self, min_time=None, max_time=None, cancel_check=None: records,
        )

        result = SessionBuilder().build(
            db,
            source_id=1,
            root_path="/tmp/videos",
            scan_mode=ScanMode.FULL,
            scan_start=datetime(2026, 3, 15, 0, 0, 0),
            scan_end=datetime(2026, 3, 16, 0, 0, 0),
        )
        db.commit()

        sessions = db.query(VideoSession).order_by(VideoSession.session_start_time.asc()).all()

        assert result.sessions_created == 2
        assert len(sessions) == 2
        assert sessions[0].session_start_time == base
        assert sessions[0].session_end_time == base + timedelta(seconds=60)
        assert sessions[0].total_duration_seconds == 60
        assert sessions[1].session_start_time == base + timedelta(seconds=62)
        assert sessions[1].session_end_time == base + timedelta(seconds=122)
        assert sessions[1].total_duration_seconds == 60
    finally:
        db.close()


def test_build_uses_coverage_duration_instead_of_sum_of_file_durations(monkeypatch) -> None:
    db = _new_db_session()
    try:
        base = datetime(2026, 3, 15, 9, 4, 4)
        records = [
            _record(base, "a"),
            _record(base + timedelta(seconds=61), "b"),
            _record(base + timedelta(seconds=122), "c"),
        ]

        monkeypatch.setattr(
            "src.services.session_builder.XiaomiDirectoryParser.scan_directory",
            lambda self, min_time=None, max_time=None, cancel_check=None: records,
        )

        SessionBuilder().build(
            db,
            source_id=1,
            root_path="/tmp/videos",
            scan_mode=ScanMode.FULL,
            scan_start=datetime(2026, 3, 15, 0, 0, 0),
            scan_end=datetime(2026, 3, 16, 0, 0, 0),
        )
        db.commit()

        session = db.query(VideoSession).one()

        assert session.total_duration_seconds == 182
        assert session.total_duration_seconds != 180
    finally:
        db.close()
