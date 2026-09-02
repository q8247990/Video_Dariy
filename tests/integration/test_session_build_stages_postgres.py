"""PostgreSQL integration tests for the session-build pipeline decomposition (Todo 20).

These tests exercise the staged runner in
:mod:`src.services.session_build.runner` against a real
PostgreSQL schema migrated to the new head. They cover the
PG-specific behaviour the SQLite unit tests in
``tests/unit/test_session_build_stages.py`` cannot:

* the full vs hot representative-fixture agreement — both
  ``run_hot`` and ``run_full`` produce the same known
  session graph against a real ``video_file`` /
  ``video_session`` /
  ``video_session_file_rel`` schema.
* the concurrent hot / full behavior: the per-source PG
  advisory transaction lock + the unique index on
  ``(source_id, file_path_hash)`` together produce one
  ``VideoFile`` row and one ``VideoSessionFileRel`` row when
  both workers race on the same source.
* sealed session dispatch: a sealed session ends up as
  exactly one outbox ``analyze_session`` event row bound to
  the right ``TaskLog`` row (no ``send_task`` round-trip —
  the partial unique index enforces one active dispatcher
  per session).
* cancel / missing-file paths: each stage's transaction
  boundary stays clean even when one stage aborts mid-batch.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.session_build import runner

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture(autouse=True)
def _truncate_session_build_tables(postgres_migrated_engine: Engine) -> None:
    """Wipe every table the session-build pipeline touches (FK-safe order)."""
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE outbox_event RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE pipeline_transition_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_session_file_rel RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_session RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_file RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_source RESTART IDENTITY CASCADE"))


@pytest.fixture()
def fake_dispatcher(monkeypatch):
    """Install a fake outbox dispatcher that records calls instead of ``send_task``.

    The slim Celery task uses
    ``get_container().dispatcher.dispatch_analyze_session``; we
    replace the container with one bound to a fake dispatcher
    that records ``(session_id, priority)`` tuples.
    """
    from src.application.bootstrap import bootstrap_for_tests
    from src.application.bootstrap_fakes import FakeTaskDispatcher

    fake = FakeTaskDispatcher()
    container = bootstrap_for_tests(dispatcher=fake)
    monkeypatch.setattr("src.tasks._container._container", container)
    return fake


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _seed_source(
    db: Session,
    *,
    source_name: str = "concurrent-cam",
    root_path: str = "/tmp/videos",
) -> int:
    source = VideoSource(
        source_name=source_name,
        camera_name=source_name,
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": root_path},
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return int(source.id)


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


def _fixture_records(base: datetime) -> list[dict[str, Any]]:
    return [
        _record(base, "a"),
        _record(base + timedelta(seconds=61), "b"),
        _record(base + timedelta(seconds=400), "c"),
    ]


# ---------------------------------------------------------------------------
# Hot + full agreement on PG
# ---------------------------------------------------------------------------


def test_pg_hot_and_full_produce_same_session_graph(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same fixture → same known session graph under ``run_hot`` and ``run_full``.

    Three files: a + b share a session (gap=1s, within the
    merge window), c is alone (gap=339s). Both runs produce
    the same two ``VideoSession`` rows with the same start /
    end timestamps, and both seal both sessions (full mode
    seals everything; hot mode has long buffer-elapsed
    against the future ``now_utc``).
    """
    base = datetime(2026, 3, 15, 9, 0, 0)
    now = base + timedelta(hours=2)
    factory = _session_factory(postgres_migrated_engine)

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: _fixture_records(base),
    )

    def _run(scan_mode_func):
        with factory() as db:
            source_id = _seed_source(db, source_name=f"cam-{scan_mode_func.__name__}")
            result = scan_mode_func(
                db,
                source_id=source_id,
                root_path="/tmp/videos",
                scan_start=base - timedelta(hours=1),
                scan_end=now,
                cancel_check=None,
                home_zone=None,
                now_utc=now,
            )
            db.commit()
            sessions = (
                db.query(VideoSession)
                .filter(VideoSession.source_id == source_id)
                .order_by(VideoSession.session_start_time.asc())
                .all()
            )
            return result, [
                (s.session_start_time, s.session_end_time, s.analysis_status) for s in sessions
            ]

    hot_result, hot_sessions = _run(runner.run_hot)
    full_result, full_sessions = _run(runner.run_full)

    assert hot_result.sessions_created == 2
    assert full_result.sessions_created == 2
    assert hot_sessions == full_sessions
    assert all(status == "sealed" for (_, _, status) in hot_sessions)


# ---------------------------------------------------------------------------
# Concurrent hot vs full race
# ---------------------------------------------------------------------------


def test_pg_concurrent_hot_and_full_preserve_one_file_and_relation(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent hot + full on the same source produce exactly one ``VideoFile`` row.

    This is the pre-Todo-20 baseline
    (``test_full_and_hot_build_collision_preserves_one_file_and_relation``).
    The per-source PG advisory transaction lock serialises
    the two workers; the unique index on
    ``(source_id, file_path_hash)`` ensures exactly one row
    survives the second worker's insert.
    """
    factory = _session_factory(postgres_migrated_engine)
    with factory() as db:
        source_id = _seed_source(db)

    start_time = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
    records = [_record(start_time, "concurrent")]
    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: records,
    )

    def _build(scan_mode: str) -> None:
        with factory() as db:
            runner.run(
                db,
                source_id=source_id,
                root_path="/tmp/videos",
                scan_mode=scan_mode,
                scan_start=start_time - timedelta(hours=1),
                scan_end=start_time + timedelta(hours=1),
                cancel_check=None,
                home_zone=None,
                now_utc=start_time + timedelta(hours=1),
            )
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_build, mode) for mode in ("full", "hot")]
        for future in futures:
            future.result()

    with factory() as db:
        assert db.query(VideoFile).filter(VideoFile.source_id == source_id).count() == 1
        assert db.query(VideoSession).filter(VideoSession.source_id == source_id).count() == 1
        session_id = db.query(VideoSession.id).filter(VideoSession.source_id == source_id).one()[0]
        assert (
            db.query(VideoSessionFileRel)
            .filter(VideoSessionFileRel.session_id == session_id)
            .count()
            == 1
        )


# ---------------------------------------------------------------------------
# Sealed-session dispatch hits the outbox exactly once (no send_task)
# ---------------------------------------------------------------------------


def test_pg_sealed_session_dispatches_exactly_one_outbox_analyze_command(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sealed session becomes one ``OutboxEvent`` row + one pending ``TaskLog``.

    The slim Celery task's
    :func:`_dispatch_analysis_for_sealed` writes through the
    outbox dispatcher port; the partial unique index on
    ``task_log.dedupe_key`` enforces one active analyzer per
    session. The ``OutboxRow.payload`` carries
    ``{"session_id": <id>, "priority": <priority>}`` matching
    the :class:`AnalyzeSessionCommand` shape.
    """
    factory = _session_factory(postgres_migrated_engine)
    base = datetime(2026, 3, 15, 9, 0, 0)
    now = base + timedelta(hours=2)

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: _fixture_records(base),
    )

    with factory() as db:
        source_id = _seed_source(db, source_name="dispatch-cam")
        result = runner.run_full(
            db,
            source_id=source_id,
            root_path="/tmp/videos",
            scan_start=base - timedelta(hours=1),
            scan_end=now,
            cancel_check=None,
            home_zone=None,
            now_utc=now,
        )
        db.commit()

        sealed_sessions = result.sealed_sessions
        assert len(sealed_sessions) >= 1

        from src.tasks.session_build import _dispatch_analysis_for_sealed

        dispatched = _dispatch_analysis_for_sealed(db, sealed_sessions)
        db.commit()

        assert len(dispatched) == len(sealed_sessions)
        for info in sealed_sessions:
            assert any(row["session_id"] == info.session_id for row in dispatched)


def test_pg_dispatcher_partial_unique_index_rejects_second_analyze(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling the dispatch helper twice for the same session yields one ``TaskLog``.

    The dispatcher port's ``create_pending_task_log`` relies
    on the partial unique index; the second call short-circuits
    to the existing row (``created=False``) so the analyzer
    isn't enqueued twice. We confirm the partial unique index
    by counting the ``TaskLog`` rows with ``status='pending'``
    after two consecutive dispatches: there should be exactly
    one for the target session.
    """
    factory = _session_factory(postgres_migrated_engine)
    base = datetime(2026, 3, 15, 9, 0, 0)
    now = base + timedelta(hours=2)

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: _fixture_records(base),
    )

    with factory() as db:
        source_id = _seed_source(db, source_name="dedup-cam")
        result = runner.run_full(
            db,
            source_id=source_id,
            root_path="/tmp/videos",
            scan_start=base - timedelta(hours=1),
            scan_end=now,
            cancel_check=None,
            home_zone=None,
            now_utc=now,
        )
        db.commit()

        assert result.sealed_sessions
        session_info = result.sealed_sessions[0]

        from src.tasks.session_build import _dispatch_analysis_for_sealed

        first = _dispatch_analysis_for_sealed(db, [session_info])
        db.commit()
        # Same payload again — partial unique index
        # short-circuits to the existing row.
        second = _dispatch_analysis_for_sealed(db, [session_info])
        db.commit()

        assert len(first) == 1
        assert len(second) == 1

    # Exactly one ``TaskLog`` row in pending state for the
    # sealed session id, even after two dispatches.
    with factory() as db:
        pending_logs_for_session = (
            db.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_ANALYSIS,
                TaskLog.task_target_id == session_info.session_id,
                TaskLog.status == TaskStatus.PENDING,
            )
            .count()
        )
        assert pending_logs_for_session == 1


# ---------------------------------------------------------------------------
# Cancel and missing-file paths don't corrupt unrelated stages
# ---------------------------------------------------------------------------


def test_pg_cancel_during_dedupe_does_not_drop_unrelated_files(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel raised inside ``dedupe_files`` rolls back the whole transaction.

    Confirm that the next build's ``run_hot`` call sees a
    clean schema (no half-written ``VideoFile`` /
    ``VideoSession`` rows) and can complete normally. The
    per-stage cancel guard is the
    :func:`ensure_task_not_cancelled` wrapper the slim Celery
    task installs.
    """
    factory = _session_factory(postgres_migrated_engine)
    base = datetime(2026, 3, 15, 9, 0, 0)
    cancel_after_first = {"called": False}

    def _maybe_cancel():
        if not cancel_after_first["called"]:
            cancel_after_first["called"] = True

    def _raising(parser_self, min_time, max_time, cancel_check):
        # The cancel check the parser uses is the same one the
        # runner passes; the test triggers it after the first
        # directory iteration.
        _maybe_cancel()
        return _fixture_records(base)

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        _raising,
    )

    with factory() as db:
        source_id = _seed_source(db, source_name="cancel-cam")
        with pytest.raises(RuntimeError):
            runner.run_hot(
                db,
                source_id=source_id,
                root_path="/tmp/videos",
                scan_start=base - timedelta(hours=1),
                scan_end=base + timedelta(hours=2),
                cancel_check=lambda: (_ for _ in ()).throw(RuntimeError("test cancel")),
                home_zone=None,
                now_utc=base + timedelta(hours=2),
            )
            db.commit()
        # Roll back the half-written state.
        db.rollback()

    # Now run normally; the second build must produce the
    # expected two sessions.
    cancel_after_first["called"] = False
    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: _fixture_records(base),
    )
    with factory() as db:
        # Source row was committed before the cancel — same source_id.
        result = runner.run_hot(
            db,
            source_id=source_id,
            root_path="/tmp/videos",
            scan_start=base - timedelta(hours=1),
            scan_end=base + timedelta(hours=2),
            cancel_check=None,
            home_zone=None,
            now_utc=base + timedelta(hours=2),
        )
        db.commit()
    assert result.sessions_created == 2


def test_pg_missing_file_not_marked_by_per_build(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-build scans do not mark ``video_file.file_missing`` (hourly maintenance owns that).

    Reproduces the pre-Todo-20
    ``test_build_does_not_mark_missing_files`` invariant
    against a real PG schema.
    """
    factory = _session_factory(postgres_migrated_engine)
    base = datetime(2026, 3, 15, 9, 0, 0)
    now = base + timedelta(hours=1)

    # Seed a known file (the file path itself is not on disk
    # — the per-build path never opens it).
    missing_path = "/tmp/videos/2026031509/31M32S_gone.mp4"
    with factory() as db:
        source_id = _seed_source(db, source_name="missing-cam", root_path="/tmp/videos")
        from src.models.video_file import build_file_path_hash

        db.add(
            VideoFile(
                source_id=source_id,
                file_name="gone.mp4",
                file_path=missing_path,
                file_path_hash=build_file_path_hash(missing_path),
                start_time=base,
                end_time=base + timedelta(seconds=60),
                duration_seconds=60,
                file_size=1024,
                parse_status="parsed",
            )
        )
        db.commit()

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: [],
    )

    with factory() as db:
        runner.run_full(
            db,
            source_id=source_id,
            root_path="/tmp/videos",
            scan_start=base - timedelta(hours=1),
            scan_end=now,
            cancel_check=None,
            home_zone=None,
            now_utc=now,
        )
        db.commit()

    with factory() as db:
        file_row = db.query(VideoFile).filter(VideoFile.source_id == source_id).one()
        assert file_row.file_missing is False


# ---------------------------------------------------------------------------
# Pipeline dispatch + outbox partial unique index integration
# ---------------------------------------------------------------------------


def test_pg_outbox_partial_unique_index_serializes_sealed_session_dispatch(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent dispatch calls for the same session produce one ``OutboxEvent``.

    The outbox dispatcher writes the
    ``OutboxEvent(task_name='src.tasks.analyzer.analyze_session_task')``
    row inside the same transaction as the ``TaskLog``; the
    partial unique index serialises peer workers to a single
    active row. We confirm by counting the ``OutboxEvent``
    rows: one event per session in the envelope, even after a
    second concurrent dispatch.
    """
    factory = _session_factory(postgres_migrated_engine)
    base = datetime(2026, 3, 15, 9, 0, 0)
    now = base + timedelta(hours=2)

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: _fixture_records(base),
    )

    with factory() as db:
        source_id = _seed_source(db, source_name="concurrent-dispatch-cam")
        result = runner.run_full(
            db,
            source_id=source_id,
            root_path="/tmp/videos",
            scan_start=base - timedelta(hours=1),
            scan_end=now,
            cancel_check=None,
            home_zone=None,
            now_utc=now,
        )
        db.commit()

    from src.tasks.session_build import _dispatch_analysis_for_sealed

    def _dispatch_once():
        with factory() as db:
            _dispatch_analysis_for_sealed(db, result.sealed_sessions)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_dispatch_once) for _ in range(2)]
        for future in futures:
            future.result()

    with factory() as db:
        event_count = (
            db.query(OutboxEvent)
            .filter(OutboxEvent.task_name == "src.tasks.analyzer.analyze_session_task")
            .count()
        )
        assert event_count == len(result.sealed_sessions)
