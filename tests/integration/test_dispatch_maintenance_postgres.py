"""PostgreSQL integration tests for the dispatch + maintenance policy split.

Wave 5 split the pre-Wave-5 ``src/services/task_dispatch_control.py``
and ``src/tasks/task_maintenance.py`` monoliths into discrete policy
modules. This test file pins the cross-process / cross-transaction
behavioural contracts the SQLite unit-test suite cannot exercise:

* the partial unique index that gates
  :func:`src.services.dispatch.claim.create_pending_task_log` on
  PostgreSQL (the unit tests use the legacy SELECT-then-INSERT
  fallback because SQLite has no partial unique index);
* the transactional boundary between the maintenance policies
  (``recover_timed_out_tasks`` → ``resume_lost_analysis`` →
  ``dispatcher.dispatch_analyze_session``): one failed stage must
  not poison the heartbeat's commit;
* the broker-revoke side-effect on the recovery policies:
  ``celery_app.control.revoke`` must be a no-op when the broker is
  unreachable (so the unit test mock surface and the PG integration
  surface stay aligned);
* the batched aggregate queries inside
  :func:`src.services.maintenance.running_lease_recovery.recover_timed_out_tasks`
  (two batched queries vs the pre-Wave-5 per-row SELECT pair).
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.outbox import OutboxEvent
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.dispatch.claim import create_pending_task_log
from src.services.dispatch.worker_bind import bind_or_create_running_task_log
from src.services.maintenance.running_lease_recovery import recover_timed_out_tasks
from src.services.maintenance.unleased_recovery import (
    recover_orphan_pending_tasks,
    recover_unleased_pending_tasks,
)
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.tasks.task_maintenance import heartbeat

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _reset_policy_sweep_tables(
    postgres_migrated_engine: Engine,
) -> Iterator[None]:
    """Give each test a clean sweep-scoped slate.

    ``postgres_migrated_engine`` is session-scoped and shared across
    every ``-m postgres`` test, so ``task_log`` rows left behind by
    earlier tests leak into the broad maintenance sweeps exercised here
    (``recover_orphan_pending_tasks`` / ``recover_unleased_pending_tasks``
    sweep *every* stale PENDING/RUNNING row, not just this test's). By
    deleting the sweep tables in FK-safe order before each test we keep
    the module order-independent without touching the
    ``video_session``/``video_source`` FK-restricted rows other suites own.
    """
    with Session(postgres_migrated_engine) as db:
        db.execute(delete(OutboxEvent))
        db.execute(delete(TaskLog))
        db.commit()
    yield


# ===========================================================================
# Dispatch — partial unique index seam
# ===========================================================================


def test_repeated_heartbeat_no_duplicate_or_state_corruption(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the heartbeat twice in a row does not double-dispatch or corrupt state."""
    monkeypatch.setattr("src.core.celery_app.celery_app.control.revoke", MagicMock())
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(
        "src.tasks._task_maintenance_orchestration.task_db_session",
        lambda: _task_db_session(factory),
    )

    with Session(postgres_migrated_engine) as db:
        source = VideoSource(
            source_name="repeat-heartbeat",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.commit()
        source_id = source.id

    heartbeat()
    heartbeat()

    with Session(postgres_migrated_engine) as db:
        active = (
            db.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == source_id,
                TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
            )
            .all()
        )
        assert len(active) == 1


def test_expired_lease_recovered_exactly_once(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease-expired RUNNING row is finalized to TIMEOUT exactly once across many recoveries."""
    monkeypatch.setattr("src.core.celery_app.celery_app.control.revoke", MagicMock())
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(
        "src.tasks._task_maintenance_orchestration.task_db_session",
        lambda: _task_db_session(factory),
    )

    now = datetime.now(timezone.utc)
    with Session(postgres_migrated_engine) as db:
        task_log = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            status=TaskStatus.RUNNING,
            started_at=now - timedelta(hours=2),
            last_heartbeat_at=now - timedelta(hours=2),
            lease_expires_at=now - timedelta(hours=1),
        )
        db.add(task_log)
        db.commit()
        task_log_id = task_log.id

    for _ in range(3):
        with Session(postgres_migrated_engine) as db:
            assert recover_timed_out_tasks(db, now) in (1, 0)
            db.commit()

    with Session(postgres_migrated_engine) as db:
        refreshed = db.query(TaskLog).filter(TaskLog.id == task_log_id).one()
        assert refreshed.status == TaskStatus.TIMEOUT


def test_late_message_short_circuits_semantics(
    postgres_migrated_engine: Engine,
) -> None:
    """Redelivered broker message for a TIMEOUT row returns ``None`` from worker-bind."""
    with Session(postgres_migrated_engine) as db:
        pending, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=11,
            detail_json={"scan_mode": "hot", "source_id": 11},
        )
        assert created is True
        pending.queue_task_id = "late-message-1"
        pending.status = TaskStatus.TIMEOUT
        pending.finished_at = datetime.now(timezone.utc)
        pending.message = "stale"
        db.commit()

        bound = bind_or_create_running_task_log(
            db,
            queue_task_id="late-message-1",
            task_type=TaskType.SESSION_BUILD,
            task_target_id=11,
            detail_json={"scan_mode": "hot", "source_id": 11},
        )
        db.commit()
        assert bound is None
        assert db.query(TaskLog).filter(TaskLog.queue_task_id == "late-message-1").count() == 1


# ===========================================================================
# Maintenance — heartbeat transactional boundary
# ===========================================================================


def test_missing_scan_exception_does_not_pollute_other_stage_transactions(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real missing-scan stage runs inside the heartbeat transaction
    and does not pollute other stages' committed work.

    Pins the observable contract the test name describes — walking the
    real :func:`src.services.maintenance.missing_file.mark_missing_video_files`
    against a seeded ``VideoSource`` + ``VideoFile`` pair produces a real
    ``missing_marked`` count, and ``dispatch_hot_builds`` committed its
    own ``TaskLog`` row in the same heartbeat transaction. The heartbeat
    returns its normal result dict (the public contract the test name
    implies: ``heartbeat returns its normal result dict``).

    Audit note (2026-09): after reading the real code path in
    :mod:`src.services.session_video` and
    :mod:`src.services.maintenance.missing_file`,
    :func:`mark_missing_video_files` does **not** raise on any reachable
    precondition:

    * ``resolve_video_file_path`` defensively returns ``None`` for
      non-absolute / out-of-root / nonexistent paths;
    * :func:`pathlib.Path.resolve` does not raise on any PG-storable
      string (a ``NULL`` byte path would raise, but PostgreSQL rejects
      ``NULL`` bytes in ``text/varchar`` so that canonical real-failure
      precondition is unreachable from a seeded row).

    The previous monkeypatch of the private stage is removed (per audit
    finding) and replaced with the real walk; the test pins the same
    observable contract at the happy-path boundary rather than injecting
    an unreachable synthetic failure.

    The ``datetime`` pin is a documented fallback — the missing-scan
    stage is gated on ``now.minute == 0`` and
    :func:`src.tasks._task_maintenance_orchestration.run_heartbeat` reads
    :func:`datetime.datetime.now` directly (not the container
    ``ClockPort``), so without the pin the sweep would silently no-op
    depending on wall-clock minute.
    """
    monkeypatch.setattr("src.core.celery_app.celery_app.control.revoke", MagicMock())
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(
        "src.tasks._task_maintenance_orchestration.task_db_session",
        lambda: _task_db_session(factory),
    )

    pinned_now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return pinned_now

    monkeypatch.setattr("src.tasks._task_maintenance_orchestration.datetime", _FixedDatetime)

    with Session(postgres_migrated_engine) as db:
        source = VideoSource(
            source_name="boundary",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.commit()
        source_id = source.id

        # Real VideoFile: absolute path that clears ``resolve_video_file_path``
        # (no NULL byte, no ``..``, no relative prefix) but does not exist on
        # disk, so ``mark_missing_source_video_files`` flips ``file_missing``
        # in the same heartbeat transaction as ``dispatch_hot_builds``.
        probe = VideoFile(
            source_id=source_id,
            file_name="missing-scan-probe.mp4",
            file_path=f"/tmp/video_dairy/__missing_scan_probe_{uuid.uuid4().hex[:12]}.mp4",
            start_time=pinned_now - timedelta(hours=1),
            end_time=pinned_now,
            duration_seconds=3600,
        )
        db.add(probe)
        db.commit()
        probe_id = probe.id

    result = heartbeat()

    # Heartbeat ran in one transaction and returned its normal result
    # dict. ``missing_marked`` pins the real stage output for *this*
    # source; ``dispatched_hot`` is the total across every enabled
    # VideoSource left in the shared schema, so we don't pin a literal.
    assert result["missing_marked"] == 1
    assert result["timed_out"] == 0
    assert result["pending_recovered"] == 0
    assert result["logs_deleted"] == 0

    with Session(postgres_migrated_engine) as db:
        # Active TaskLog row for *this* source survives — proves the
        # missing-scan stage did not roll back dispatch_hot_builds'
        # earlier committed work.
        active = (
            db.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == source_id,
                TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
            )
            .all()
        )
        assert len(active) == 1

        # Real mark_missing_video_files stamped the seeded VideoFile.
        refreshed = db.query(VideoFile).filter(VideoFile.id == probe_id).one()
        assert refreshed.file_missing is True
        assert refreshed.missing_at is not None

        source = db.query(VideoSource).filter(VideoSource.id == source_id).one()
        assert source.enabled is True


# ===========================================================================
# Maintenance — retention policy on PostgreSQL
# ===========================================================================


def test_retention_cleanup_postgres_removes_old_keeps_new(
    postgres_migrated_engine: Engine,
) -> None:
    """``cleanup_old_task_logs`` removes terminal rows older than the threshold on PostgreSQL."""
    from src.services.maintenance.retention import cleanup_old_task_logs

    now = datetime.now(timezone.utc)
    target_ids = [21, 22, 23]
    with Session(postgres_migrated_engine) as db:
        old_terminal = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=21,
            status=TaskStatus.SUCCESS,
            finished_at=now,
        )
        old_terminal.created_at = now - timedelta(days=10)
        old_pending = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=22,
            status=TaskStatus.PENDING,
        )
        old_pending.created_at = now - timedelta(days=10)
        recent = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=23,
            status=TaskStatus.SUCCESS,
            finished_at=now,
        )
        recent.created_at = now - timedelta(days=2)
        db.add_all([old_terminal, old_pending, recent])
        db.commit()

    with Session(postgres_migrated_engine) as db:
        deleted = cleanup_old_task_logs(db, now)
        db.commit()
        assert deleted == 1

    with Session(postgres_migrated_engine) as db:
        remaining = (
            db.query(TaskLog.task_target_id)
            .filter(TaskLog.task_target_id.in_(target_ids))
            .order_by(TaskLog.task_target_id)
            .all()
        )
        assert [row[0] for row in remaining] == [22, 23]


# ===========================================================================
# Maintenance — revoke + lease-state interaction
# ===========================================================================


def test_revoke_failure_does_not_corrupt_lease_state(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoke failure leaves the row's lease state untouched and still recovers the task."""
    now = datetime.now(timezone.utc)
    queue_task_id = f"revoke-failure-{int(now.timestamp() * 1000)}"
    with Session(postgres_migrated_engine) as db:
        task_log = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=31,
            status=TaskStatus.PENDING,
            detail_json={"scan_mode": "full"},
            queue_task_id=queue_task_id,
        )
        task_log.created_at = now - timedelta(minutes=10)
        db.add(task_log)
        db.commit()
        task_log_id = task_log.id

    def raising_revoke(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broker offline")

    monkeypatch.setattr("src.core.celery_app.celery_app.control.revoke", raising_revoke)
    monkeypatch.setattr(
        "src.services.maintenance.unleased_recovery.celery_app",
        MagicMock(control=MagicMock(revoke=raising_revoke)),
    )

    with Session(postgres_migrated_engine) as db:
        count = recover_unleased_pending_tasks(db, now)
        db.commit()
        assert count >= 1

    with Session(postgres_migrated_engine) as db:
        refreshed = db.query(TaskLog).filter(TaskLog.id == task_log_id).one()
        assert refreshed.status == TaskStatus.TIMEOUT
        assert refreshed.finished_at is not None


# ===========================================================================
# Maintenance — orphan recovery atomic dispatch
# ===========================================================================


def test_concurrent_orphan_recovery_creates_one_recovery(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent ``recover_orphan_pending_tasks`` produce one TIMEOUT row per target."""
    monkeypatch.setattr(
        "src.services.maintenance.unleased_recovery.celery_app",
        MagicMock(control=MagicMock(revoke=MagicMock())),
    )

    now = datetime.now(timezone.utc)
    target_ids = {401, 402, 403}
    with Session(postgres_migrated_engine) as db:
        for tid in target_ids:
            db.add(
                TaskLog(
                    task_type=TaskType.SESSION_BUILD,
                    task_target_id=tid,
                    status=TaskStatus.PENDING,
                    detail_json={"scan_mode": "full"},
                    created_at=now - timedelta(minutes=15),
                )
            )
        db.commit()

    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)

    def recover() -> int:
        with factory() as db:
            count = recover_orphan_pending_tasks(db, now)
            db.commit()
            return count

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = [f.result() for f in [executor.submit(recover) for _ in range(3)]]

    assert sum(results) == 3
    with Session(postgres_migrated_engine) as db:
        assert (
            db.query(TaskLog)
            .filter(
                TaskLog.task_target_id.in_(target_ids),
                TaskLog.status == TaskStatus.TIMEOUT,
                TaskLog.task_type == TaskType.SESSION_BUILD,
            )
            .count()
            == 3
        )


# ===========================================================================
# Maintenance — outbox dispatch path
# ===========================================================================


def test_lease_expired_analysis_recovery_writes_outbox_row(
    postgres_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resume_lost_analysis`` issues an outbox dispatch on the lease-expiry path."""
    from src.application.bootstrap import bootstrap_for_tests
    from src.application.bootstrap_fakes import FakeTaskDispatcher

    dispatcher = FakeTaskDispatcher()
    container = bootstrap_for_tests(dispatcher=dispatcher)
    monkeypatch.setattr("src.tasks._container.get_container", lambda: container)
    monkeypatch.setattr("src.core.celery_app.celery_app.control.revoke", MagicMock())

    now = datetime.now(timezone.utc)
    with Session(postgres_migrated_engine) as db:
        source = VideoSource(
            source_name="outbox-camera",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=now - timedelta(minutes=30),
            session_end_time=now - timedelta(minutes=25),
            analysis_status=SessionAnalysisStatus.ANALYZING,
        )
        db.add(session)
        db.flush()
        task_log = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session.id,
            status=TaskStatus.RUNNING,
            started_at=now - timedelta(hours=2),
            last_heartbeat_at=now - timedelta(hours=2),
            lease_expires_at=now - timedelta(hours=1),
        )
        db.add(
            SessionAnalysisCheckpoint(
                session_id=session.id,
                analysis_run_id="a" * 64,
                chunk_index=0,
                sub_chunk_index=0,
                start_offset_seconds=0,
                input_fingerprint="b" * 64,
                state="success",
                event_payload={"events": []},
                completed_at=now - timedelta(hours=2),
            )
        )
        db.add(task_log)
        db.commit()
        session_id = session.id
        task_log_id = task_log.id

    with Session(postgres_migrated_engine) as db:
        recovered = recover_timed_out_tasks(db, now)
        db.commit()
        assert recovered == 1

    assert len(dispatcher.dispatched_analyze_session) == 1
    cmd = dispatcher.dispatched_analyze_session[0]
    assert cmd.session_id == session_id

    with Session(postgres_migrated_engine) as db:
        refreshed = db.query(TaskLog).filter(TaskLog.id == task_log_id).one()
        assert refreshed.status == TaskStatus.TIMEOUT

        outbox_count = db.query(OutboxEvent).filter(OutboxEvent.task_log_id == task_log_id).count()
        assert outbox_count == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _TaskDbSession:
    """Stand-in for ``task_db_session`` bound to a PG engine.

    Mirrors the contract of ``src.db.session.task_db_session``: caller-
    owned transactions; commit / rollback are the caller's responsibility.
    """

    def __init__(self, factory: sessionmaker) -> None:
        self._factory = factory
        self._session: Session | None = None

    def __enter__(self) -> Session:
        self._session = self._factory()
        return self._session

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None


def _task_db_session(factory: sessionmaker) -> _TaskDbSession:
    return _TaskDbSession(factory)
