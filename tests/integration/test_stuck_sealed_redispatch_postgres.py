"""PostgreSQL integration tests: stuck-sealed session self-heal via the hot build.

Regression coverage for the split-commit failure where a build's seal
transition committed but the analyzer dispatch never landed (worker
death between the two commits, or a swallowed dispatch error). Such a
session sits in ``SEALED`` with no ``session_analysis`` TaskLog and no
mechanism would ever re-dispatch it.

The fix rides the existing hot build (heartbeat-driven, every ~60s per
source): the build re-dispatches ``SEALED`` sessions that carry no
``session_analysis`` TaskLog at all, inside the same transaction that
commits the build itself (seal + handoff are now atomic).

These tests run the real :func:`run_hot_build` orchestration with the
production dispatcher (outbox rows, real partial unique index).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401 — registers every model on Base.metadata
from src.application.outbox import contracts as outbox_contracts
from src.models.outbox import OutboxEvent
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.session_build.persistence import find_stuck_sealed_sessions
from src.tasks.session_build import run_hot_build

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture(autouse=True)
def _truncate_session_build_tables(postgres_migrated_engine: Engine) -> None:
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE outbox_event RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE pipeline_transition_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_session_file_rel RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_session RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_file RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_source RESTART IDENTITY CASCADE"))


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _seed_source(db: Session, source_name: str = "stuck-cam") -> int:
    source = VideoSource(
        source_name=source_name,
        camera_name=source_name,
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp/videos"},
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return int(source.id)


def _seed_session(
    db: Session,
    source_id: int,
    *,
    start_time: datetime,
    status: str,
    priority: str | None = None,
) -> int:
    session = VideoSession(
        source_id=source_id,
        session_start_time=start_time,
        session_end_time=start_time + timedelta(minutes=30),
        total_duration_seconds=1800,
        analysis_status=status,
        analysis_priority=priority,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return int(session.id)


def _seed_analysis_task_log(db: Session, session_id: int, status: str) -> None:
    db.add(
        TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session_id,
            dedupe_key=f"session_analysis|{session_id}",
            status=status,
            detail_json={"priority": "hot", "dedupe_key": f"session_analysis|{session_id}"},
        )
    )
    db.commit()


def _analysis_task_logs(db: Session, session_id: int) -> list[TaskLog]:
    return (
        db.query(TaskLog)
        .filter(
            TaskLog.task_type == TaskType.SESSION_ANALYSIS,
            TaskLog.task_target_id == session_id,
        )
        .all()
    )


def _analysis_outboxes(db: Session, session_id: int) -> list[OutboxEvent]:
    task_log_ids = [log.id for log in _analysis_task_logs(db, session_id)]
    return (
        db.query(OutboxEvent).filter(OutboxEvent.task_log_id.in_(task_log_ids)).all()
        if task_log_ids
        else []
    )


def _record(start_time: datetime, suffix: str) -> dict[str, Any]:
    return {
        "file_name": f"{suffix}.mp4",
        "file_path": f"/tmp/videos/{suffix}.mp4",
        "start_time": start_time,
        "end_time": start_time + timedelta(seconds=60),
        "duration_seconds": 60,
        "file_size": 1024,
        "file_format": "mp4",
        "storage_type": "local_file",
    }


# ---------------------------------------------------------------------------
# Stuck-seal self-heal through the existing hot build
# ---------------------------------------------------------------------------


def test_pg_hot_build_redispatches_stuck_sealed_session(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SEALED session with no analysis TaskLog is re-dispatched by the next hot build."""
    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: [],
    )
    factory = _session_factory(postgres_migrated_engine)
    with factory() as db:
        source_id = _seed_source(db)
        base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
        stuck_id = _seed_session(db, source_id, start_time=base, status="sealed")

        result = run_hot_build(db, source_id=source_id, queue_task_id="")

        assert result["analysis_redispatched"] == 1
        logs = _analysis_task_logs(db, stuck_id)
        assert len(logs) == 1
        assert logs[0].status == TaskStatus.PENDING
        outboxes = _analysis_outboxes(db, stuck_id)
        assert len(outboxes) == 1
        assert outboxes[0].queue == "analysis_hot"

        build_log = (
            db.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == source_id,
            )
            .one()
        )
        assert build_log.status == TaskStatus.SUCCESS
        assert build_log.detail_json["analysis_redispatched"] == 1


def test_pg_hot_build_honors_stuck_session_analysis_priority(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-dispatch uses the priority recorded at seal time (full queue for full scans)."""
    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: [],
    )
    factory = _session_factory(postgres_migrated_engine)
    with factory() as db:
        source_id = _seed_source(db)
        base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
        stuck_id = _seed_session(db, source_id, start_time=base, status="sealed", priority="full")

        run_hot_build(db, source_id=source_id, queue_task_id="")

        outboxes = _analysis_outboxes(db, stuck_id)
        assert len(outboxes) == 1
        assert outboxes[0].queue == "analysis_full"


def test_pg_hot_build_skips_sealed_session_with_active_analysis_task(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SEALED session with an in-flight analysis task is left alone (no duplicate dispatch)."""
    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: [],
    )
    factory = _session_factory(postgres_migrated_engine)
    with factory() as db:
        source_id = _seed_source(db)
        base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
        queued_id = _seed_session(db, source_id, start_time=base, status="sealed")
        _seed_analysis_task_log(db, queued_id, TaskStatus.PENDING)

        result = run_hot_build(db, source_id=source_id, queue_task_id="")

        assert result["analysis_redispatched"] == 0
        assert len(_analysis_task_logs(db, queued_id)) == 1
        assert len(_analysis_outboxes(db, queued_id)) == 0


def test_pg_hot_build_skips_recovery_exhausted_session(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SEALED session with a terminal TaskLog (recovery budget exhausted) is not auto-retried."""
    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: [],
    )
    factory = _session_factory(postgres_migrated_engine)
    with factory() as db:
        source_id = _seed_source(db)
        base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
        exhausted_id = _seed_session(db, source_id, start_time=base, status="sealed")
        _seed_analysis_task_log(db, exhausted_id, TaskStatus.TIMEOUT)

        result = run_hot_build(db, source_id=source_id, queue_task_id="")

        assert result["analysis_redispatched"] == 0
        assert len(_analysis_task_logs(db, exhausted_id)) == 1


def test_pg_hot_build_seals_new_and_redispatches_stuck_without_duplicates(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freshly sealed sessions and stuck sessions are each dispatched exactly once, atomically."""
    base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
    records = [_record(base, "a"), _record(base + timedelta(seconds=61), "b")]
    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: records,
    )
    factory = _session_factory(postgres_migrated_engine)
    with factory() as db:
        source_id = _seed_source(db)
        stuck_id = _seed_session(
            db, source_id, start_time=base - timedelta(days=1), status="sealed"
        )

        result = run_hot_build(db, source_id=source_id, queue_task_id="")

        fresh_ids = [
            row[0]
            for row in db.query(VideoSession.id).filter(
                VideoSession.source_id == source_id,
                VideoSession.id != stuck_id,
            )
        ]
        assert len(fresh_ids) == 1

        assert result["analysis_redispatched"] == 1
        assert result["analysis_dispatched"] == 2

        for session_id in [*fresh_ids, stuck_id]:
            logs = _analysis_task_logs(db, session_id)
            assert len(logs) == 1, f"session {session_id} dispatched {len(logs)} times"
            assert len(_analysis_outboxes(db, session_id)) == 1

        build_log = (
            db.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == source_id,
            )
            .one()
        )
        assert build_log.status == TaskStatus.SUCCESS


# ---------------------------------------------------------------------------
# The stuck-sweep predicate itself
# ---------------------------------------------------------------------------


def test_pg_find_stuck_sealed_sessions_predicate(postgres_migrated_engine: Engine) -> None:
    """Only SEALED sessions without any session_analysis TaskLog are reported stuck."""
    factory = _session_factory(postgres_migrated_engine)
    with factory() as db:
        source_id = _seed_source(db)
        other_source_id = _seed_source(db, source_name="other-cam")
        base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)

        stuck = _seed_session(db, source_id, start_time=base, status="sealed")
        queued = _seed_session(db, source_id, start_time=base + timedelta(hours=1), status="sealed")
        _seed_analysis_task_log(db, queued, TaskStatus.PENDING)
        exhausted = _seed_session(
            db, source_id, start_time=base + timedelta(hours=2), status="sealed"
        )
        _seed_analysis_task_log(db, exhausted, TaskStatus.TIMEOUT)
        open_id = _seed_session(db, source_id, start_time=base + timedelta(hours=3), status="open")
        analyzing_id = _seed_session(
            db, source_id, start_time=base + timedelta(hours=4), status="analyzing"
        )
        success_id = _seed_session(
            db, source_id, start_time=base + timedelta(hours=5), status="success"
        )
        other_source_stuck = _seed_session(db, other_source_id, start_time=base, status="sealed")

        found = find_stuck_sealed_sessions(db, source_id)

        assert [session.id for session in found] == [stuck]
        # The other source's stuck session is out of scope for this source's build.
        assert [session.id for session in find_stuck_sealed_sessions(db, other_source_id)] == [
            other_source_stuck
        ]
        assert open_id not in {session.id for session in found}
        assert analyzing_id not in {session.id for session in found}
        assert success_id not in {session.id for session in found}
        assert queued not in {session.id for session in found}
        assert exhausted not in {session.id for session in found}
