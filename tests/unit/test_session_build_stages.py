"""Unit tests for the session-build pipeline stage decomposition (Todo 20).

白盒测试：直接调用内部 stage 函数/类，断言绑定实现细节，随实现重构，不作为接口契约回归基线。

These tests exercise the public API of
:mod:`src.services.session_build` end-to-end against the project-wide
PostgreSQL ``pg_db`` / ``pg_db_factory`` fixtures. They cover the
per-stage contracts the slim Celery task in
:mod:`src.tasks.session_build` depends on, but that the legacy
``tests/unit/test_session_builder.py`` suite did not pin explicitly:

* :func:`reducer.reduce_files` — the **pure** reducer.
  Callers feed it in-memory records (no DB) and the function
  must produce the same session graph for hot and full builds.
* :func:`seal_policy.select_sessions_to_seal` — the **pure**
  half of the seal stage. Tests pin the open→sealed decision
  for ``ScanMode.HOT`` (latest-keep-all-else-seal) and
  ``ScanMode.FULL`` (seal everything), plus the buffer
  fallback (``SEAL_BUFFER_SECONDS`` elapsed).
* :func:`dedupe.dedupe_files` — the file-path hash dedupe.
  Existing rows are skipped; the ``(source_id, file_path_hash)``
  unique index race is silent; post-dedupe ``InsertedFile``
  list is returned.
* :func:`runner.run` / :func:`run_hot` / :func:`run_full` —
  the whole pipeline composition. The hot and full variants
  produce the same session graph for a representative fixture
  (the ``test_run_hot_and_full_agree_on_session_graph``
  contract).

The PostgreSQL-specific concurrency / partial-unique-index /
advisory-lock semantics live in
``tests/integration/test_session_build_stages_postgres.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.models.video_file import VideoFile, build_file_path_hash
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import ScanMode, SessionAnalysisStatus
from src.services.session_build import (
    EXTEND,
    NEW_SESSION,
    DiscoveredFile,
    dedupe,
    reducer,
    runner,
    seal_policy,
)
from src.services.session_build.constants import (
    MERGE_GAP_SECONDS,
    SEAL_BUFFER_SECONDS,
)

# ---------------------------------------------------------------------------
# PostgreSQL session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(pg_db: Session) -> Session:
    """Single session against the project-wide ``pg_db`` fixture."""
    return pg_db


@pytest.fixture
def engine_factory(pg_db_factory: Callable[[], Session]):
    """Yield a ``(session_factory_callable,)`` the multi-session test uses.

    The single test that needs an independent session (``hot`` + ``full``)
    is wired up to call ``factory()`` once per scenario; the resulting
    sessions land on the same PostgreSQL schema and clean up together
    via ``pg_db_factory``'s TRUNCATE step.
    """

    def _factory() -> tuple[Any, Callable[[], Session]]:
        # The legacy SQLite version returned ``(engine, sessionmaker)``.
        # Under PostgreSQL we share a single engine across every
        # ``session_factory()`` call, so the first tuple slot is ``None``
        # (callers that previously used ``engine.dispose()`` are gone
        # because ``pg_engine`` is session-scoped via the fixture).
        return None, pg_db_factory

    return _factory


# ---------------------------------------------------------------------------
# In-memory record helpers used by the pure reducer tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeVideoFile:
    """A record-shaped stand-in for the :class:`VideoFile` model the reducer reads."""

    id: int
    start_time: datetime
    end_time: datetime


@dataclass(frozen=True)
class FakeInsertedFile:
    """The reducer's input shape: paired file-path hash + ``video_file`` proxy."""

    file_path_hash: str
    video_file: FakeVideoFile


@dataclass(frozen=True)
class FakeExistingSession:
    """A snapshot of the source's still-open :class:`VideoSession`."""

    id: int
    session_end_time: datetime


def _record(start: datetime, hash_id: int) -> FakeInsertedFile:
    return FakeInsertedFile(
        file_path_hash=f"hash-{hash_id}",
        video_file=FakeVideoFile(
            id=hash_id,
            start_time=start,
            end_time=start + timedelta(seconds=60),
        ),
    )


# ---------------------------------------------------------------------------
# Pure reducer
# ---------------------------------------------------------------------------


def test_reducer_one_second_gap_emits_single_extend_session() -> None:
    """Two files with a 1-second gap extend the same brand-new session.

    Traces the first integration of :func:`reduce_files` —
    the reducer tracks a "current session" across the merge
    window and merges the files into one session that the
    persistence stage creates in ``plan.new_sessions``.
    """
    base = datetime(2026, 3, 15, 9, 0, 0)
    inserted = [
        _record(base, 1),
        _record(base + timedelta(seconds=61), 2),
    ]
    plan = reducer.reduce_files(inserted, merge_gap_seconds=MERGE_GAP_SECONDS)

    assert plan.extend_session_id is None
    assert len(plan.new_sessions) == 1
    assert len(plan.append_actions) == 2
    assert plan.append_actions[0].target_session == NEW_SESSION
    assert plan.append_actions[0].sort_index == 0
    assert plan.append_actions[1].target_session == NEW_SESSION
    assert plan.append_actions[1].new_session_index == 0
    assert plan.append_actions[1].sort_index == 1


def test_reducer_over_gap_splits_into_separate_sessions() -> None:
    """Two files whose gap exceeds the merge window split into separate sessions.

    This is the pre-Todo-20 invariant:
    ``test_build_splits_sessions_when_gap_exceeds_one_second``.
    With pure records the reducer must emit two
    ``new_sessions`` entries plus two ``append_actions`` each
    targeting their own session index.
    """
    base = datetime(2026, 3, 15, 9, 0, 0)
    inserted = [
        _record(base, 1),
        _record(base + timedelta(seconds=62), 2),
    ]
    plan = reducer.reduce_files(inserted, merge_gap_seconds=MERGE_GAP_SECONDS)

    assert len(plan.new_sessions) == 2
    assert len(plan.append_actions) == 2
    assert plan.append_actions[0].target_session == NEW_SESSION
    assert plan.append_actions[1].target_session == NEW_SESSION
    assert plan.append_actions[0].new_session_index == 0
    assert plan.append_actions[1].new_session_index == 1
    assert plan.append_actions[0].sort_index == 0
    assert plan.append_actions[1].sort_index == 0


def test_reducer_out_of_order_falls_back_to_chronological() -> None:
    """Discovery sorts by ``start_time`` before the reducer runs.

    Confirm the reducer still produces the right graph when
    the input is pre-sorted (the production invariant). An
    out-of-order input would produce a wrong graph; the
    contract is "the caller must sort".
    """
    base = datetime(2026, 3, 15, 9, 0, 0)
    inserted = [
        _record(base, 1),
        _record(base + timedelta(seconds=61), 2),
        _record(base + timedelta(seconds=122), 3),
    ]
    plan = reducer.reduce_files(inserted, merge_gap_seconds=MERGE_GAP_SECONDS)

    assert len(plan.new_sessions) == 1
    assert len(plan.append_actions) == 3


def test_reducer_with_existing_open_session_extends_it() -> None:
    """When the source has a still-OPEN session, the reducer extends it.

    The reducer compares against the snapshot's
    ``session_end_time``; first file within the merge window
    emits an ``EXTEND`` action with ``target_session`` =
    :data:`EXTEND` sentinel.
    """
    base = datetime(2026, 3, 15, 9, 0, 0)
    existing = FakeExistingSession(
        id=42,
        session_end_time=base,
    )
    inserted = [
        _record(base + timedelta(seconds=1), 100),
        _record(base + timedelta(seconds=62), 101),
    ]
    plan = reducer.reduce_files(
        inserted,
        existing_open_session=existing,
        merge_gap_seconds=MERGE_GAP_SECONDS,
    )

    assert plan.extend_session_id == 42
    assert plan.append_actions[0].target_session == EXTEND
    assert plan.append_actions[0].sort_index == 0
    assert plan.append_actions[1].target_session == EXTEND
    assert plan.append_actions[1].sort_index == 1
    assert plan.new_sessions == []
    assert plan.next_sort_index == 2


def test_reducer_with_existing_open_session_out_of_gap_creates_new_session() -> None:
    """Files after the existing session's end_time+merge window open a new session.

    The reducer keeps the pre-existing session untouched
    (no ``EXTEND`` action emitted) and seeds a brand-new
    session for the new batch.
    """
    base = datetime(2026, 3, 15, 9, 0, 0)
    existing = FakeExistingSession(id=42, session_end_time=base)
    inserted = [
        _record(base + timedelta(seconds=120), 200),
        _record(base + timedelta(seconds=181), 201),
    ]
    plan = reducer.reduce_files(
        inserted,
        existing_open_session=existing,
        merge_gap_seconds=MERGE_GAP_SECONDS,
    )

    assert plan.extend_session_id == 42
    assert len(plan.new_sessions) == 1
    assert plan.append_actions[0].target_session == NEW_SESSION
    assert plan.append_actions[0].new_session_index == 0
    assert plan.append_actions[1].target_session == NEW_SESSION
    assert plan.append_actions[1].sort_index == 1


def test_reducer_empty_input_returns_empty_plan() -> None:
    """No files → no work. The reducer still records the existing session."""
    existing = FakeExistingSession(id=99, session_end_time=datetime(2026, 1, 1, 0, 0, 0))
    plan = reducer.reduce_files([], existing_open_session=existing)
    assert plan.extend_session_id == 99
    assert plan.new_sessions == []
    assert plan.append_actions == []


# ---------------------------------------------------------------------------
# Pure seal policy
# ---------------------------------------------------------------------------


def _fake_open_session(session_id: int, session_end_time: datetime) -> Any:
    """A record-shaped stand-in for :class:`VideoSession` the seal policy reads."""

    class _Stub:
        pass

    stub = _Stub()
    stub.id = session_id
    stub.session_end_time = session_end_time
    stub.session_start_time = session_end_time - timedelta(seconds=60)
    return stub


def test_seal_policy_full_mode_seals_every_open_session() -> None:
    now = datetime(2026, 3, 15, 12, 0, 0)
    open_sessions = [
        _fake_open_session(1, datetime(2026, 3, 15, 10, 0, 0)),
        _fake_open_session(2, datetime(2026, 3, 15, 9, 0, 0)),
        _fake_open_session(3, datetime(2026, 3, 15, 11, 0, 0)),
    ]
    decision = seal_policy.select_sessions_to_seal(
        scan_mode=ScanMode.FULL,
        open_sessions=open_sessions,
        now_utc=now,
    )
    assert sorted(decision.sealed_session_ids) == [1, 2, 3]


def test_seal_policy_hot_mode_seals_all_but_latest() -> None:
    now = datetime(2026, 3, 15, 12, 0, 0)
    latest_end = datetime(2026, 3, 15, 11, 0, 0)
    # latest_end + buffer = 11:10 < 12:00, so the buffer has elapsed → 2 is sealed too.
    open_sessions = [
        _fake_open_session(1, datetime(2026, 3, 15, 10, 0, 0)),
        _fake_open_session(2, latest_end),
        _fake_open_session(3, datetime(2026, 3, 15, 9, 0, 0)),
    ]
    decision = seal_policy.select_sessions_to_seal(
        scan_mode=ScanMode.HOT,
        open_sessions=open_sessions,
        now_utc=now,
    )
    assert sorted(decision.sealed_session_ids) == [1, 2, 3]


def test_seal_policy_hot_mode_seals_prior_sessions_keeps_latest_in_buffer() -> None:
    now = datetime(2026, 3, 15, 12, 0, 0)
    # Latest is 11:30 — buffer would expire at 11:40; 11:40 < 12:00 → elapsed → 2 IS sealed.
    # Use 11:55 → buffer expires 12:05 — within buffer (not sealed).
    open_sessions = [
        _fake_open_session(1, datetime(2026, 3, 15, 10, 0, 0)),
        _fake_open_session(2, datetime(2026, 3, 15, 11, 55, 0)),
        _fake_open_session(3, datetime(2026, 3, 15, 9, 0, 0)),
    ]
    decision = seal_policy.select_sessions_to_seal(
        scan_mode=ScanMode.HOT,
        open_sessions=open_sessions,
        now_utc=now,
    )
    assert sorted(decision.sealed_session_ids) == [1, 3]


def test_seal_policy_hot_mode_keeps_latest_open_when_buffer_not_elapsed() -> None:
    now = datetime(2026, 3, 15, 12, 0, 0)
    latest_end = now - timedelta(seconds=SEAL_BUFFER_SECONDS - 30)
    open_sessions = [
        _fake_open_session(1, datetime(2026, 3, 15, 10, 0, 0)),
        _fake_open_session(2, latest_end),
    ]
    decision = seal_policy.select_sessions_to_seal(
        scan_mode=ScanMode.HOT,
        open_sessions=open_sessions,
        now_utc=now,
    )
    assert decision.sealed_session_ids == [1]


def test_seal_policy_hot_mode_seals_latest_when_buffer_elapsed() -> None:
    now = datetime(2026, 3, 15, 12, 0, 0)
    latest_end = now - timedelta(seconds=SEAL_BUFFER_SECONDS + 30)
    open_sessions = [
        _fake_open_session(1, datetime(2026, 3, 15, 10, 0, 0)),
        _fake_open_session(2, latest_end),
    ]
    decision = seal_policy.select_sessions_to_seal(
        scan_mode=ScanMode.HOT,
        open_sessions=open_sessions,
        now_utc=now,
    )
    assert sorted(decision.sealed_session_ids) == [1, 2]


def test_seal_policy_hot_mode_with_single_session_buffer_only() -> None:
    now = datetime(2026, 3, 15, 12, 0, 0)
    only_end = now - timedelta(seconds=SEAL_BUFFER_SECONDS + 10)
    open_sessions = [_fake_open_session(1, only_end)]
    decision = seal_policy.select_sessions_to_seal(
        scan_mode=ScanMode.HOT,
        open_sessions=open_sessions,
        now_utc=now,
    )
    assert decision.sealed_session_ids == [1]


def test_seal_policy_hot_mode_empty_returns_empty() -> None:
    decision = seal_policy.select_sessions_to_seal(
        scan_mode=ScanMode.HOT,
        open_sessions=[],
        now_utc=datetime(2026, 3, 15, 12, 0, 0),
    )
    assert decision.sealed_session_ids == []


# ---------------------------------------------------------------------------
# Hash dedupe
# ---------------------------------------------------------------------------


def _discovered(start_time: datetime, suffix: str) -> DiscoveredFile:
    return DiscoveredFile(
        file_name=f"{suffix}.mp4",
        file_path=f"/tmp/videos/{suffix}.mp4",
        start_time=start_time,
        end_time=start_time + timedelta(seconds=60),
        duration_seconds=60,
        file_size=1024,
    )


def _seed_source(db_session) -> int:
    """Create a parent ``VideoSource`` and return its server-assigned id."""
    source = VideoSource(
        source_name="cam",
        camera_name="cam",
        location_name="home",
        source_type="local_directory",
        enabled=True,
    )
    db_session.add(source)
    db_session.flush()
    return source.id


def test_dedupe_skips_existing_files(db_session) -> None:
    source_id = _seed_source(db_session)
    base = datetime(2026, 3, 15, 9, 0, 0)
    file_a = _discovered(base, "a")
    db_session.add(
        VideoFile(
            source_id=source_id,
            file_name=file_a.file_name,
            file_path=file_a.file_path,
            file_path_hash=build_file_path_hash(file_a.file_path),
            start_time=file_a.start_time,
            end_time=file_a.end_time,
            duration_seconds=60,
            file_size=1024,
            parse_status="parsed",
        )
    )
    db_session.commit()

    file_b = _discovered(base + timedelta(seconds=61), "b")
    inserted = dedupe.dedupe_files(
        db_session,
        source_id=source_id,
        discovered_files=[file_a, file_b],
    )
    db_session.commit()

    assert len(inserted) == 1
    assert inserted[0].file_path_hash == build_file_path_hash(file_b.file_path)
    assert db_session.query(VideoFile).count() == 2


def test_dedupe_inserts_new_files(db_session) -> None:
    source_id = _seed_source(db_session)
    base = datetime(2026, 3, 15, 9, 0, 0)
    files = [
        _discovered(base, "a"),
        _discovered(base + timedelta(seconds=61), "b"),
    ]
    inserted = dedupe.dedupe_files(
        db_session,
        source_id=source_id,
        discovered_files=files,
    )
    db_session.commit()

    assert len(inserted) == 2
    assert db_session.query(VideoFile).count() == 2
    assert all(isinstance(item.video_file, VideoFile) for item in inserted)


def test_dedupe_marks_existing_hashes_without_query(db_session) -> None:
    """Pre-computed ``existing_hashes`` short-circuits the dedupe query path."""
    source_id = _seed_source(db_session)
    base = datetime(2026, 3, 15, 9, 0, 0)
    file_a = _discovered(base, "a")
    file_a_hash = build_file_path_hash(file_a.file_path)
    inserted = dedupe.dedupe_files(
        db_session,
        source_id=source_id,
        discovered_files=[file_a, _discovered(base + timedelta(seconds=61), "b")],
        existing_hashes={file_a_hash},
    )
    db_session.commit()
    assert len(inserted) == 1
    assert inserted[0].file_path_hash != file_a_hash
    assert db_session.query(VideoFile).count() == 1


def test_dedupe_unique_index_race_silently_skips(db_session) -> None:
    """When two workers race to insert the same file, the runner sees ``None``."""
    source_id = _seed_source(db_session)
    base = datetime(2026, 3, 15, 9, 0, 0)
    file_a = _discovered(base, "a")
    db_session.add(
        VideoFile(
            source_id=source_id,
            file_name=file_a.file_name,
            file_path=file_a.file_path,
            file_path_hash=build_file_path_hash(file_a.file_path),
            start_time=file_a.start_time,
            end_time=file_a.end_time,
            duration_seconds=60,
            file_size=1024,
            parse_status="parsed",
        )
    )
    db_session.commit()

    # ``existing_hashes`` is empty (simulating the race where
    # both workers started their queries before the unique
    # row was committed). The savepoint around the
    # ``INSERT`` in ``insert_file`` swallows the
    # ``IntegrityError`` and the dedupe returns ``None``.
    # We can't easily race two threads on SQLite, so we
    # assert the call returns no rows for file_a.
    inserted = dedupe.dedupe_files(
        db_session,
        source_id=source_id,
        discovered_files=[file_a],
        existing_hashes=set(),
    )
    db_session.rollback()
    assert inserted == []


# ---------------------------------------------------------------------------
# Whole-pipeline composition (run_hot / run_full agreement)
# ---------------------------------------------------------------------------


@pytest.fixture
def monkeypatch_xiaomi_parser(monkeypatch):
    """Replace ``XiaomiDirectoryParser.scan_directory`` with a stub returning the staged records.

    Returns the captured-call list so tests can inspect the
    bounds the slim Celery task would have used.
    """
    captured: dict = {}

    def _factory(parser_self, min_time, max_time, cancel_check):
        captured["min_time"] = min_time
        captured["max_time"] = max_time
        return captured.pop("records", [])

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        _factory,
    )
    return captured


def test_run_hot_and_full_agree_on_session_graph(db_session, monkeypatch, engine_factory) -> None:
    """Same fixture produces the same session graph under hot and full runs.

    The two runs use different scan windows but the dedupe /
    reducer / seal stages compose deterministically: a
    representative fixture of 3 files (one merge gap, one
    over-gap) must yield the same ``(sessions, rels,
    sealed_sessions)`` under either scan mode.
    """
    base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
    records = [
        {
            "file_name": "a.mp4",
            "file_path": "/tmp/videos/a.mp4",
            "start_time": base,
            "end_time": base + timedelta(seconds=60),
            "duration_seconds": 60,
            "file_size": 1024,
            "file_format": "mp4",
            "storage_type": "local_file",
        },
        {
            "file_name": "b.mp4",
            "file_path": "/tmp/videos/b.mp4",
            "start_time": base + timedelta(seconds=61),
            "end_time": base + timedelta(seconds=121),
            "duration_seconds": 60,
            "file_size": 1024,
            "file_format": "mp4",
            "storage_type": "local_file",
        },
        {
            "file_name": "c.mp4",
            "file_path": "/tmp/videos/c.mp4",
            "start_time": base + timedelta(seconds=400),
            "end_time": base + timedelta(seconds=460),
            "duration_seconds": 60,
            "file_size": 1024,
            "file_format": "mp4",
            "storage_type": "local_file",
        },
    ]

    def _stub(parser_self, min_time, max_time, cancel_check):
        return records

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        _stub,
    )

    def _run(scan_mode_func):
        _engine_unused, session_factory = engine_factory()
        session = session_factory()
        try:
            session.add(
                VideoSource(
                    source_name="cam",
                    camera_name="cam",
                    location_name="客厅",
                    source_type="local_directory",
                    config_json={"root_path": "/tmp/videos"},
                    enabled=True,
                )
            )
            session.commit()
            source_id = session.query(VideoSource).order_by(VideoSource.id.desc()).first().id
            result = scan_mode_func(
                session,
                source_id=source_id,
                root_path="/tmp/videos",
                scan_start=base - timedelta(hours=1),
                scan_end=base + timedelta(hours=2),
                cancel_check=None,
                home_zone=None,
                now_utc=base + timedelta(hours=2),
            )
            session.commit()
            return (
                result,
                [
                    (s.session_start_time, s.session_end_time)
                    for s in session.query(VideoSession)
                    .filter(VideoSession.source_id == source_id)
                    .order_by(VideoSession.session_start_time.asc())
                    .all()
                ],
            )
        finally:
            session.close()

    hot_result, hot_sessions = _run(runner.run_hot)
    full_result, full_sessions = _run(runner.run_full)

    # Hot + full should both create 2 sessions (a+b merged,
    # c alone) and seal them (the buffer has long elapsed
    # against our far-future ``now_utc``).
    assert hot_result.sessions_created == 2
    assert full_result.sessions_created == 2
    assert hot_sessions == full_sessions


def test_run_hot_preserves_existing_open_session_end(db_session, monkeypatch) -> None:
    """The HOT-mode build appends to the existing OPEN session if the gap is inside the window.

    Reproduces the pre-Todo-20 ``test_build_does_not_merge_older_files_into_latest_open_session``
    invariant against the slim runner.
    """
    source_id = _seed_source(db_session)
    # The PG DateTime(timezone=True) columns come back tz-aware, so
    # the in-test datetimes must be tz-aware too — otherwise the
    # reducer's ``(start - end).total_seconds()`` raises a TypeError.
    base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
    open_session = VideoSession(
        source_id=source_id,
        session_start_time=base,
        session_end_time=base + timedelta(seconds=60),
        total_duration_seconds=60,
        analysis_status=SessionAnalysisStatus.OPEN,
        analysis_priority="hot",
    )
    db_session.add(open_session)
    db_session.commit()

    records = [
        {
            "file_name": "b.mp4",
            "file_path": "/tmp/videos/b.mp4",
            "start_time": base + timedelta(seconds=120),
            "end_time": base + timedelta(seconds=180),
            "duration_seconds": 60,
            "file_size": 1024,
            "file_format": "mp4",
            "storage_type": "local_file",
        },
        {
            "file_name": "c.mp4",
            "file_path": "/tmp/videos/c.mp4",
            "start_time": base + timedelta(seconds=181),
            "end_time": base + timedelta(seconds=241),
            "duration_seconds": 60,
            "file_size": 1024,
            "file_format": "mp4",
            "storage_type": "local_file",
        },
    ]

    def _stub(parser_self, min_time, max_time, cancel_check):
        return records

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        _stub,
    )

    result = runner.run_hot(
        db_session,
        source_id=source_id,
        root_path="/tmp/videos",
        scan_start=base - timedelta(hours=1),
        scan_end=base + timedelta(hours=1),
        cancel_check=None,
        home_zone=None,
        now_utc=base + timedelta(hours=2),
    )
    db_session.commit()

    sessions = (
        db_session.query(VideoSession)
        .filter(VideoSession.source_id == source_id)
        .order_by(VideoSession.id.asc())
        .all()
    )
    # b opens a new session (gap=60s from open), c merges into b (gap=1s)
    assert result.sessions_created == 1
    assert result.sessions_updated == 2
    assert len(sessions) == 2
    assert sessions[0].id == open_session.id
    assert sessions[1].session_start_time == base + timedelta(seconds=120)
    assert sessions[1].session_end_time == base + timedelta(seconds=241)


def test_run_empty_hot_mode_seals_buffer_elapsed_session(db_session, monkeypatch) -> None:
    """When discovery returns nothing in HOT mode, the seal-buffer sweep
    seals stale OPEN sessions."""
    source_id = _seed_source(db_session)
    base = datetime(2026, 3, 15, 9, 0, 0)
    stale_end = base - timedelta(seconds=SEAL_BUFFER_SECONDS + 30)
    open_session = VideoSession(
        source_id=source_id,
        session_start_time=stale_end - timedelta(seconds=60),
        session_end_time=stale_end,
        total_duration_seconds=60,
        analysis_status=SessionAnalysisStatus.OPEN,
        analysis_priority="hot",
    )
    db_session.add(open_session)
    db_session.commit()

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda *args, **kwargs: [],
    )

    result = runner.run_hot(
        db_session,
        source_id=source_id,
        root_path="/tmp/videos",
        scan_start=base - timedelta(seconds=1),
        scan_end=base,
        cancel_check=None,
        home_zone=None,
        now_utc=base,
    )
    db_session.commit()

    db_session.refresh(open_session)
    assert open_session.analysis_status == SessionAnalysisStatus.SEALED
    assert result.sessions_sealed == 1


def test_run_does_not_mark_missing_files_in_per_build() -> None:
    """Per-build scans never mark ``video_file.file_missing`` (the hourly maintenance owns it)."""
    plan = reducer.reduce_files([])
    assert plan.new_sessions == []
    # The hourly ``mark_missing_video_files`` sweep is owned by
    # :mod:`src.tasks.task_maintenance` (see
    # :class:`src.services.maintenance.missing_file.mark_missing_video_files`)
    # and intentionally absent from the per-build path.
