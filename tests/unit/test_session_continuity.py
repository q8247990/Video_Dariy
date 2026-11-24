from datetime import datetime, timedelta

from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis.continuity import load_previous_session_subjects


def _source(db, name: str = "客厅") -> VideoSource:
    source = VideoSource(
        source_name=name,
        camera_name=name,
        location_name=name,
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.flush()
    return source


def _session(
    db,
    source: VideoSource,
    start: datetime,
    subjects: list[str],
) -> VideoSession:
    session = VideoSession(
        source_id=source.id,
        session_start_time=start,
        session_end_time=start + timedelta(seconds=60),
        total_duration_seconds=60,
        analysis_status="sealed",
        main_subjects_json=subjects,
    )
    db.add(session)
    db.flush()
    return session


def test_previous_session_subjects_uses_immediately_preceding_same_source(pg_db) -> None:
    db = pg_db
    base = datetime(2026, 11, 24, 21, 0, 0)
    source = _source(db)
    other_source = _source(db, "厨房")
    _session(db, source, base, ["嘟嘟", "嘟嘟", " ", "墨墨"])
    target = _session(db, source, base + timedelta(minutes=5), [])
    _session(db, source, base + timedelta(minutes=10), ["飞飞"])
    _session(db, other_source, base + timedelta(minutes=1), ["陌生人"])

    assert load_previous_session_subjects(db, target) == ["嘟嘟", "墨墨"]


def test_previous_session_subjects_is_empty_without_predecessor(pg_db) -> None:
    db = pg_db
    source = _source(db)
    target = _session(db, source, datetime(2026, 11, 24, 21, 0, 0), [])

    assert load_previous_session_subjects(db, target) == []


def test_previous_session_subjects_ignores_non_list_payload(pg_db) -> None:
    db = pg_db
    base = datetime(2026, 11, 24, 21, 0, 0)
    source = _source(db)
    previous = _session(db, source, base, [])
    previous.main_subjects_json = {"unexpected": "shape"}
    db.flush()
    target = _session(db, source, base + timedelta(minutes=5), [])

    assert load_previous_session_subjects(db, target) == []


def test_previous_session_subjects_caps_at_eight(pg_db) -> None:
    db = pg_db
    base = datetime(2026, 11, 24, 21, 0, 0)
    source = _source(db)
    names = [f"对象{i}" for i in range(10)]
    _session(db, source, base, names)
    target = _session(db, source, base + timedelta(minutes=5), [])

    assert load_previous_session_subjects(db, target) == names[:8]
