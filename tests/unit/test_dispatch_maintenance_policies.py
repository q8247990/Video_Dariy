"""Tests covering every dispatch + maintenance policy module independently.

The pre-Wave-5 ``src/services/task_dispatch_control.py`` and
``src/tasks/task_maintenance.py`` were monolithic; Wave 5 split
them into discrete policy modules under
:mod:`src.services.dispatch` and :mod:`src.services.maintenance`.
This test file pins every split so a future refactor cannot
collapse the policies back into a monolith without a failing
test.

Each policy module is exercised against the project-wide
``pg_db`` PostgreSQL fixture (auto-marked as a postgres test by
``tests/conftest.py``). The behavioural specs in
``tests/unit/test_task_maintenance.py`` and
``tests/integration/test_dispatch_maintenance_postgres.py`` pin
the end-to-end outcome; this file pins the policy-module surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from src.application import container_holder as _container
from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeTaskDispatcher
from src.core.pipeline_constants import (
    ScanMode,
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.models.outbox import OutboxEvent
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.dispatch.cancel import (
    TaskCancellationRequested,
    ensure_task_not_cancelled,
    get_task_log_for_update,
    is_task_cancel_requested,
)
from src.services.dispatch.claim import create_pending_task_log
from src.services.dispatch.dedupe import (
    build_dedupe_key,
    ensure_dict_detail,
    find_duplicate_active_task,
    is_singleton_task_running,
)
from src.services.dispatch.deferred import supersede_active_hot_scan
from src.services.dispatch.finalize import (
    finalize_cancelled_task_log,
    finalize_task_log,
)
from src.services.dispatch.lease import renew_task_lease
from src.services.dispatch.worker_bind import bind_or_create_running_task_log
from src.services.maintenance.hot_scheduling import dispatch_hot_builds
from src.services.maintenance.retention import cleanup_old_task_logs
from src.services.maintenance.running_lease_recovery import recover_timed_out_tasks
from src.services.maintenance.unleased_recovery import recover_orphan_pending_tasks

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(pg_db: Session) -> Session:
    """Single session against the project-wide ``pg_db`` fixture (PostgreSQL)."""
    return pg_db


@pytest.fixture(autouse=True)
def _restore_container() -> None:
    """Always reset the task-layer container holder after each test."""
    try:
        yield
    finally:
        _container.reset_container_for_tests()


def _bind_fake_dispatcher(monkeypatch: pytest.MonkeyPatch) -> FakeTaskDispatcher:
    dispatcher = FakeTaskDispatcher()
    container = bootstrap_for_tests(dispatcher=dispatcher)
    monkeypatch.setattr("src.application.container_holder._container", container)
    monkeypatch.setattr("src.core.celery_app.celery_app.control.revoke", MagicMock())
    return dispatcher


# ===========================================================================
# Dispatch — dedupe policy
# ===========================================================================


def test_dedupe_claim_returns_existing_active(db_session: Session) -> None:
    """``find_duplicate_active_task`` returns the existing PENDING row, not a new one."""
    db = db_session
    pending, created = create_pending_task_log(
        db,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "hot", "source_id": 1},
    )
    db.commit()
    assert created is True

    duplicate = find_duplicate_active_task(
        db,
        TaskType.SESSION_BUILD,
        1,
        pending.dedupe_key or "",
    )
    assert duplicate is not None
    assert duplicate.id == pending.id


def test_dedupe_claim_returns_none_when_no_active_row(db_session: Session) -> None:
    """``find_duplicate_active_task`` returns ``None`` for a fresh dedupe-key."""
    db = db_session
    duplicate = find_duplicate_active_task(
        db,
        TaskType.SESSION_BUILD,
        99,
        "source_scan:99",
    )
    assert duplicate is None


def test_ensure_dict_detail_normalises_non_dict_input() -> None:
    """``ensure_dict_detail`` returns ``{}`` for non-dict input."""
    assert ensure_dict_detail(None) == {}
    assert ensure_dict_detail("garbage") == {}
    assert ensure_dict_detail(42) == {}
    out = ensure_dict_detail({1: "a", "b": 2})
    assert out == {"1": "a", "b": 2}


def test_build_dedupe_key_session_build_uses_source_only() -> None:
    """``session_build`` dedupes by source — hot and full share the same lock."""
    hot_key = build_dedupe_key(TaskType.SESSION_BUILD, 1, {"scan_mode": "hot"})
    full_key = build_dedupe_key(TaskType.SESSION_BUILD, 1, {"scan_mode": "full"})
    assert hot_key == "source_scan:1"
    assert full_key == "source_scan:1"


def test_build_dedupe_key_session_analysis_uses_target() -> None:
    """``session_analysis`` dedupes by target session."""
    key = build_dedupe_key(TaskType.SESSION_ANALYSIS, 42, {"priority": "hot"})
    assert key == "session_analysis|42"


def test_build_dedupe_key_daily_summary_uses_target_date() -> None:
    """``daily_summary_generation`` dedupes by target_date."""
    key = build_dedupe_key(TaskType.DAILY_SUMMARY_GENERATION, None, {"target_date": "2026-09-02"})
    assert key == "daily_summary_generation|2026-09-02"


def test_is_singleton_task_running_scan_mode_filter(db_session: Session) -> None:
    """``is_singleton_task_running`` honours the scan_mode filter."""
    db = db_session
    create_pending_task_log(
        db,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "full", "source_id": 1},
    )
    db.commit()
    assert is_singleton_task_running(db, TaskType.SESSION_BUILD, 1, scan_mode="full") is True
    assert is_singleton_task_running(db, TaskType.SESSION_BUILD, 1, scan_mode="hot") is False


# ===========================================================================
# Dispatch — worker-bind policy
# ===========================================================================


def test_worker_bind_creates_running_row_when_nothing_active(db_session: Session) -> None:
    """``bind_or_create_running_task_log`` INSERTs a new RUNNING row when none exists."""
    db = db_session
    bound = bind_or_create_running_task_log(
        db,
        queue_task_id="queue-fresh-1",
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "hot", "source_id": 1},
    )
    db.commit()
    assert bound is not None
    assert bound.status == TaskStatus.RUNNING
    assert bound.queue_task_id == "queue-fresh-1"
    assert bound.dedupe_key == "source_scan:1"


def test_worker_bind_rebinds_running_row_for_redelivered_message(db_session: Session) -> None:
    """A redelivered broker message for an already-RUNNING row rebinds in place."""
    db = db_session
    pending, _ = create_pending_task_log(
        db,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "hot", "source_id": 1},
    )
    pending.queue_task_id = "queue-redeliver-1"
    db.commit()

    first = bind_or_create_running_task_log(
        db,
        queue_task_id="queue-redeliver-1",
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "hot", "source_id": 1},
    )
    db.commit()
    assert first is not None

    second = bind_or_create_running_task_log(
        db,
        queue_task_id="queue-redeliver-1",
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "hot", "source_id": 1},
    )
    db.commit()
    assert second is not None
    assert second.id == first.id


def test_worker_bind_creates_running_when_pending_exists(db_session: Session) -> None:
    """A pending row with the matching dedupe-key is bound into RUNNING in place."""
    db = db_session
    pending, created = create_pending_task_log(
        db,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "hot", "source_id": 1},
    )
    assert created is True
    db.commit()

    bound = bind_or_create_running_task_log(
        db,
        queue_task_id="queue-supersede-1",
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "hot", "source_id": 1},
    )
    db.commit()
    assert bound is not None
    assert bound.id == pending.id
    assert bound.status == TaskStatus.RUNNING


# ===========================================================================
# Dispatch — lease policy
# ===========================================================================


def test_lease_renew_pushes_expires_forward(db_session: Session) -> None:
    """``renew_task_lease`` extends ``lease_expires_at`` for RUNNING rows."""
    db = db_session
    original_expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.RUNNING,
        lease_owner="queue-lease-1",
        lease_expires_at=original_expiry,
    )
    db.add(task_log)
    db.commit()

    renew_task_lease(db, task_log.id, lease_owner="queue-lease-1")
    db.commit()
    db.refresh(task_log)
    assert task_log.lease_expires_at is not None
    assert _as_utc(task_log.lease_expires_at) > _as_utc(original_expiry)


def test_lease_renew_is_noop_on_non_running(db_session: Session) -> None:
    """``renew_task_lease`` does not touch terminal rows."""
    db = db_session
    original_expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.TIMEOUT,
        lease_owner="queue-lease-2",
        lease_expires_at=original_expiry,
    )
    db.add(task_log)
    db.commit()

    renew_task_lease(db, task_log.id, lease_owner="queue-lease-2")
    db.refresh(task_log)
    assert _as_utc(task_log.lease_expires_at) == _as_utc(original_expiry)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# ===========================================================================
# Dispatch — finalize policy
# ===========================================================================


def test_finalize_terminal_stamp(db_session: Session) -> None:
    """``finalize_task_log`` stamps ``finished_at`` and merges detail_json."""
    db = db_session
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.RUNNING,
        detail_json={"scan_mode": "hot"},
    )
    db.add(task_log)
    db.commit()

    finalize_task_log(
        db,
        task_log,
        TaskStatus.SUCCESS,
        message="done",
        detail_json={"extra": "info"},
    )
    db.commit()
    db.refresh(task_log)
    assert task_log.status == TaskStatus.SUCCESS
    assert task_log.message == "done"
    assert task_log.finished_at is not None
    assert task_log.detail_json["scan_mode"] == "hot"
    assert task_log.detail_json["extra"] == "info"
    assert task_log.detail_json["transition"]["to"] == "success"


def test_cancel_sets_cancel_requested(db_session: Session) -> None:
    """``finalize_cancelled_task_log`` flips ``cancel_requested=True`` + status=CANCELLED."""
    db = db_session
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.RUNNING,
    )
    db.add(task_log)
    db.commit()
    finalize_cancelled_task_log(db, task_log, message="cancelled by user")
    db.commit()
    db.refresh(task_log)
    assert task_log.status == TaskStatus.CANCELLED
    assert task_log.cancel_requested is True
    assert task_log.message == "cancelled by user"


# ===========================================================================
# Dispatch — cancel observation policy
# ===========================================================================


def test_cancel_observation_raises_on_user_request(db_session: Session) -> None:
    """``ensure_task_not_cancelled`` raises when ``cancel_requested=True``."""
    db = db_session
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.RUNNING,
        cancel_requested=True,
    )
    db.add(task_log)
    db.commit()

    assert is_task_cancel_requested(db, task_log.id) is True
    with pytest.raises(TaskCancellationRequested):
        ensure_task_not_cancelled(db, task_log.id, default_message="user pressed stop")


def test_cancel_observation_passes_when_no_request(db_session: Session) -> None:
    """``ensure_task_not_cancelled`` is a no-op when no cancellation has been requested."""
    db = db_session
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.RUNNING,
    )
    db.add(task_log)
    db.commit()
    assert is_task_cancel_requested(db, task_log.id) is False
    ensure_task_not_cancelled(db, task_log.id)


def test_get_task_log_for_update_returns_none_for_missing(db_session: Session) -> None:
    """``get_task_log_for_update`` returns ``None`` for a missing row."""
    db = db_session
    assert get_task_log_for_update(db, 999_999) is None


# ===========================================================================
# Dispatch — deferred / supersede policy
# ===========================================================================


def test_supersede_active_hot_scan_marks_cancelled(db_session: Session) -> None:
    """``supersede_active_hot_scan`` flips the in-flight HOT to CANCELLED with the flag."""
    db = db_session
    hot_log, _ = create_pending_task_log(
        db,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        detail_json={"scan_mode": "hot", "source_id": 1},
    )
    db.commit()
    supersede_active_hot_scan(db, 1, hot_log)
    db.commit()
    db.refresh(hot_log)
    assert hot_log.status == TaskStatus.CANCELLED
    assert hot_log.detail_json["superseded"] is True
    assert hot_log.detail_json["superseded_by"] == "full_scan_request"


# ===========================================================================
# Maintenance — hot scheduling policy
# ===========================================================================


def test_hot_scheduling_picks_enabled_sources(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dispatch_hot_builds`` walks every enabled, un-paused source."""
    db = db_session
    for enabled, paused in [(True, False), (True, False), (True, True), (False, False)]:
        db.add(
            VideoSource(
                source_name=f"src-{enabled}-{paused}",
                camera_name="cam",
                location_name="loc",
                source_type="local_directory",
                last_validate_status="success",
                enabled=enabled,
                source_paused=paused,
            )
        )
    db.commit()

    dispatcher = _bind_fake_dispatcher(monkeypatch)
    dispatched = dispatch_hot_builds(db, dispatcher=dispatcher)

    assert len(dispatched) == 2
    assert all(c.scan_mode == ScanMode.HOT for c in dispatcher.dispatched_session_build)


def test_hot_scheduling_skips_source_scanned_within_interval(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source scanned more recently than ``scan_interval_seconds`` is skipped."""
    db = db_session
    now = datetime.now(timezone.utc)
    db.add(
        VideoSource(
            source_name="fresh",
            camera_name="cam",
            location_name="loc",
            source_type="local_directory",
            last_validate_status="success",
            last_scan_at=now,
        )
    )
    db.add(
        VideoSource(
            source_name="stale",
            camera_name="cam",
            location_name="loc",
            source_type="local_directory",
            last_validate_status="success",
            last_scan_at=None,
        )
    )
    db.commit()

    dispatcher = _bind_fake_dispatcher(monkeypatch)
    dispatched = dispatch_hot_builds(db, dispatcher=dispatcher)

    assert len(dispatched) == 1
    assert [c.scan_mode for c in dispatcher.dispatched_session_build] == [ScanMode.HOT]


# ===========================================================================
# Maintenance — running-lease recovery policy
# ===========================================================================


def test_recovery_policy_claims_expired_lease_run(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recover_timed_out_tasks`` flips RUNNING rows whose lease expired to TIMEOUT."""
    db = db_session
    now = datetime.now(timezone.utc)
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

    monkeypatch.setattr("src.core.celery_app.celery_app.control.revoke", MagicMock())
    dispatcher = FakeTaskDispatcher()
    assert recover_timed_out_tasks(db, now, dispatcher=dispatcher) == 1
    db.commit()
    db.refresh(task_log)
    assert task_log.status == TaskStatus.TIMEOUT

    # Idempotent: a second pass on the same now must do nothing.
    assert recover_timed_out_tasks(db, now, dispatcher=dispatcher) == 0


def test_recovery_is_noop_when_lease_still_fresh(db_session: Session) -> None:
    """``recover_timed_out_tasks`` does not touch RUNNING rows whose lease is still valid."""
    db = db_session
    now = datetime.now(timezone.utc)
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.RUNNING,
        started_at=now,
        last_heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=5),
    )
    db.add(task_log)
    db.commit()
    assert recover_timed_out_tasks(db, now, dispatcher=FakeTaskDispatcher()) == 0
    db.commit()
    db.refresh(task_log)
    assert task_log.status == TaskStatus.RUNNING


# ===========================================================================
# Maintenance — unleased / orphan recovery policy
# ===========================================================================


def test_orphan_pending_recovery_returns_deterministic_count(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recover_orphan_pending_tasks`` covers both unleased + lease-expired pending."""
    monkeypatch.setattr(
        "src.services.maintenance.unleased_recovery.celery_app",
        MagicMock(control=MagicMock(revoke=MagicMock())),
    )
    db = db_session
    now = datetime.now(timezone.utc)
    db.add(
        TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            status=TaskStatus.PENDING,
            detail_json={"scan_mode": "full"},
            queue_task_id="orphan-1",
            created_at=now - timedelta(minutes=10),
        )
    )
    db.add(
        TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=2,
            status=TaskStatus.PENDING,
            detail_json={"scan_mode": "full"},
            lease_expires_at=now - timedelta(minutes=5),
            created_at=now - timedelta(hours=2),
        )
    )
    db.commit()
    assert recover_orphan_pending_tasks(db, now, dispatcher=FakeTaskDispatcher()) == 2


# ===========================================================================
# Maintenance — retention policy (Issue 3: outbox FK blocked the sweep)
# ===========================================================================


def _seed_outbox_row(
    db: Session,
    task_log_id: int | None,
    created_at: datetime,
    status: str = "published",
) -> OutboxEvent:
    """Insert one ``outbox_event`` row bound to ``task_log_id`` (or orphaned)."""
    outbox = OutboxEvent(
        event_id=uuid4(),
        task_log_id=task_log_id,
        task_name="src.tasks.session_build.hot_scan_source",
        queue="analysis",
        args_json=[],
        kwargs_json={},
        status=status,
        next_attempt_at=created_at,
        published_at=created_at if status == "published" else None,
        created_at=created_at,
    )
    db.add(outbox)
    return outbox


def test_retention_deletes_old_outbox_and_task_log_without_fk_error(
    db_session: Session,
) -> None:
    """``cleanup_old_task_logs`` prunes the old published outbox row, then the task_log.

    Regression spec for Issue 3: the old ``ON DELETE RESTRICT`` FK made the
    bulk ``DELETE`` raise ``ForeignKeyViolation`` on every hourly heartbeat.
    After migration ``20261124_0029`` (SET NULL) + outbox pre-pruning, the
    sweep succeeds, both rows are gone, and the return value counts only
    the deleted ``task_log`` rows.
    """
    db = db_session
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=8)
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.SUCCESS,
        created_at=old,
        finished_at=old,
    )
    db.add(task_log)
    db.flush()
    outbox = _seed_outbox_row(db, task_log.id, old)
    db.commit()
    task_log_id, outbox_id = task_log.id, outbox.id

    deleted = cleanup_old_task_logs(db, now)
    db.commit()

    assert deleted == 1
    assert db.query(TaskLog).filter_by(id=task_log_id).first() is None
    assert db.query(OutboxEvent).filter_by(id=outbox_id).first() is None


def test_retention_keeps_recent_outbox_rows(db_session: Session) -> None:
    """A published outbox row inside the retention window survives the sweep."""
    db = db_session
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=8)
    recent = now - timedelta(hours=1)
    stale_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.FAILED,
        created_at=old,
        finished_at=old,
    )
    fresh_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=2,
        status=TaskStatus.SUCCESS,
        created_at=recent,
        finished_at=recent,
    )
    db.add_all([stale_log, fresh_log])
    db.flush()
    _seed_outbox_row(db, stale_log.id, old)
    fresh_outbox = _seed_outbox_row(db, fresh_log.id, recent)
    db.commit()

    deleted = cleanup_old_task_logs(db, now)
    db.commit()

    assert deleted == 1
    db.refresh(fresh_outbox)
    assert fresh_outbox.task_log_id == fresh_log.id
    assert db.query(OutboxEvent).filter_by(status="published").count() == 1


def test_outbox_fk_nulls_task_log_link_on_run_delete(db_session: Session) -> None:
    """Migration 20261124_0029: deleting the task_log NULLs ``outbox_event.task_log_id``.

    The ledger row survives as a NULL-orphaned audit record instead of the
    DELETE raising under the retired RESTRICT FK.
    """
    db = db_session
    now = datetime.now(timezone.utc)
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.SUCCESS,
        created_at=now,
        finished_at=now,
    )
    db.add(task_log)
    db.flush()
    outbox = _seed_outbox_row(db, task_log.id, now)
    db.flush()
    outbox_id = outbox.id

    db.delete(task_log)
    db.flush()
    db.expire_all()

    survivor = db.query(OutboxEvent).filter_by(id=outbox_id).one()
    assert survivor.task_log_id is None


# ===========================================================================
# Maintenance — running-lease recovery: cancel_requested zombie rows (Issue 5)
# ===========================================================================


def test_recovery_finalizes_cancelled_expired_analysis_lease(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel-requested RUNNING analysis row with an expired lease → CANCELLED + session SEALED.

    Regression spec for Issue 5: a worker killed by ``docker compose down``
    left the row RUNNING forever (the old early-return skipped every
    ``cancel_requested`` row) and stranded the ``video_session`` in
    ``analyzing``. The recovery loop must now revoke, finalize the row as
    CANCELLED, roll the session ``ANALYZING -> SEALED`` and — critically —
    never re-dispatch the analyzer.
    """
    db = db_session
    now = datetime.now(timezone.utc)
    source = VideoSource(
        source_name="cam",
        camera_name="cam",
        location_name="home",
        source_type="local_directory",
        last_validate_status="success",
    )
    db.add(source)
    db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=now - timedelta(hours=3),
        session_end_time=now - timedelta(hours=2),
        analysis_status=SessionAnalysisStatus.ANALYZING,
    )
    db.add(session)
    db.flush()
    task_log = TaskLog(
        task_type=TaskType.SESSION_ANALYSIS,
        task_target_id=session.id,
        status=TaskStatus.RUNNING,
        queue_task_id="queue-cancelled-zombie",
        cancel_requested=True,
        started_at=now - timedelta(hours=2),
        last_heartbeat_at=now - timedelta(hours=2),
        lease_expires_at=now - timedelta(hours=1),
    )
    db.add(task_log)
    db.commit()

    revoke = MagicMock()
    monkeypatch.setattr("src.core.celery_app.celery_app.control.revoke", revoke)
    dispatcher = FakeTaskDispatcher()
    assert recover_timed_out_tasks(db, now, dispatcher=dispatcher) == 1
    db.commit()
    db.refresh(task_log)
    db.refresh(session)

    revoke.assert_called_once_with("queue-cancelled-zombie", terminate=True)
    assert task_log.status == TaskStatus.CANCELLED
    assert task_log.finished_at is not None
    assert task_log.message == "Task cancelled after lease expiry (worker lost)"
    assert session.analysis_status == SessionAnalysisStatus.SEALED
    # No resume dispatch may ever reach the broker for a cancelled run.
    assert dispatcher.dispatched_analyze_session == []

    # Idempotent: the row is terminal now; a second pass finalizes nothing.
    assert recover_timed_out_tasks(db, now, dispatcher=dispatcher) == 0


def test_recovery_spares_cancel_requested_row_with_live_lease(
    db_session: Session,
) -> None:
    """``cancel_requested`` with a fresh lease stays RUNNING: the worker finalizes it itself."""
    db = db_session
    now = datetime.now(timezone.utc)
    task_log = TaskLog(
        task_type=TaskType.SESSION_BUILD,
        task_target_id=1,
        status=TaskStatus.RUNNING,
        cancel_requested=True,
        started_at=now,
        last_heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=5),
    )
    db.add(task_log)
    db.commit()

    assert recover_timed_out_tasks(db, now, dispatcher=FakeTaskDispatcher()) == 0
    db.refresh(task_log)
    assert task_log.status == TaskStatus.RUNNING
    assert task_log.finished_at is None


# ===========================================================================
# Maintenance — missing-file reconciliation policy
# ===========================================================================


# ===========================================================================
# Maintenance — heartbeat aggregator
# ===========================================================================


def test_heartbeat_aggregates_results_no_branching(
    pg_db_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``heartbeat`` aggregates deterministic counters from each policy module.

    Real-DB rewrite: seeds one of each kind of recovery candidate (an
    expired-lease ``RUNNING`` row, an orphan ``PENDING`` row, a stale
    terminal row for retention, an enabled :class:`VideoSource` with a
    mix of present + missing :class:`VideoFile` rows for the missing-file
    sweep), drives the public ``heartbeat()`` entry, and asserts the
    returned counters and the persisted
    :class:`AppRuntimeState.heartbeat_last_counters` row.

    The :func:`src.tasks._task_maintenance_orchestration.datetime`
    attribute is the only environmental patch the orchestrator exposes
    for time control (the heartbeat reads ``datetime.now(timezone.utc)``
    directly; no clock port is wired yet). The patch is the minimal
    seam noted by the audit for the ``now.minute == 0`` branch.
    """
    import src.tasks._task_maintenance_orchestration as heartbeat_orchestration
    import src.tasks.task_maintenance as heartbeat_module
    from src.db.metrics import HEARTBEAT_COUNTERS_KEY
    from src.models.app_runtime_state import AppRuntimeState
    from src.models.video_file import VideoFile

    session_factory = pg_db_factory

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(heartbeat_orchestration, "task_db_session", _task_session)
    _container.set_container_for_tests(bootstrap_for_tests(dispatcher=FakeTaskDispatcher()))

    pinned_now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return pinned_now

    monkeypatch.setattr(heartbeat_orchestration, "datetime", _FixedDatetime)
    monkeypatch.setattr("src.core.config.settings.VIDEO_ROOT_PATH", str(tmp_path))

    seed_db: Session = session_factory()
    try:
        now = pinned_now
        source = VideoSource(
            source_name="cam",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            last_validate_status="success",
            enabled=True,
            source_paused=False,
        )
        seed_db.add(source)
        seed_db.flush()

        seed_db.add(
            TaskLog(
                task_type=TaskType.SESSION_BUILD,
                task_target_id=1,
                status=TaskStatus.RUNNING,
                lease_owner="queue-expired-1",
                lease_expires_at=now - timedelta(hours=1),
                started_at=now - timedelta(hours=2),
                last_heartbeat_at=now - timedelta(hours=2),
            )
        )
        seed_db.add(
            TaskLog(
                task_type=TaskType.SESSION_BUILD,
                task_target_id=2,
                status=TaskStatus.PENDING,
                queue_task_id="orphan-task-id",
                detail_json={"scan_mode": "full"},
                created_at=now - timedelta(hours=2),
            )
        )
        seed_db.add(
            TaskLog(
                task_type=TaskType.SESSION_BUILD,
                task_target_id=99,
                status=TaskStatus.SUCCESS,
                created_at=now - timedelta(days=8),
                finished_at=now - timedelta(days=8),
            )
        )

        (tmp_path / "20260101").mkdir()
        (tmp_path / "20260101" / "present.mp4").write_bytes(b"present")
        seed_db.add_all(
            [
                VideoFile(
                    source_id=source.id,
                    file_name="present.mp4",
                    file_path=str(tmp_path / "20260101" / "present.mp4"),
                    start_time=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                    end_time=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
                    duration_seconds=60,
                ),
                VideoFile(
                    source_id=source.id,
                    file_name="gone.mp4",
                    file_path=str(tmp_path / "20260101" / "gone.mp4"),
                    start_time=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
                    end_time=datetime(2026, 1, 1, 0, 2, 0, tzinfo=timezone.utc),
                    duration_seconds=60,
                ),
            ]
        )
        seed_db.commit()
    finally:
        seed_db.close()

    result = heartbeat_module.heartbeat()

    expected = {
        "dispatched_hot": 1,
        "timed_out": 1,
        "pending_recovered": 1,
        "logs_deleted": 1,
        "missing_marked": 1,
        "redelivered": 0,
    }
    actual = {k: result[k] for k in expected}
    assert actual == expected

    verify_db: Session = session_factory()
    try:
        persisted = (
            verify_db.query(AppRuntimeState).filter_by(state_key=HEARTBEAT_COUNTERS_KEY).one()
        )
        assert persisted.state_value == expected
    finally:
        verify_db.close()


def test_heartbeat_skips_hourly_stages_outside_minute_zero(
    pg_db_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``heartbeat`` runs the per-tick sweeps unconditionally and gates the hourly
    ``cleanup_old_task_logs`` + ``mark_missing_video_files`` stages on
    ``now.minute == 0``.

    Real-DB rewrite: seeds an expired lease row, an orphan pending row,
    a stale terminal row, and an enabled source with a missing-file
    candidate, then drives ``heartbeat()`` with ``now.minute = 30`` so
    the hourly stages are skipped. Asserts only the returned counter
    dict + the persisted ``AppRuntimeState.heartbeat_last_counters`` —
    no internal call counts.

    Same env patch on the orchestrator's :mod:`datetime` as the minute-zero
    test above; documented in the audit as the only available seam until
    the heartbeat reads a clock port.
    """
    import src.tasks._task_maintenance_orchestration as heartbeat_orchestration
    import src.tasks.task_maintenance as heartbeat_module
    from src.db.metrics import HEARTBEAT_COUNTERS_KEY
    from src.models.app_runtime_state import AppRuntimeState
    from src.models.video_file import VideoFile

    session_factory = pg_db_factory

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(heartbeat_orchestration, "task_db_session", _task_session)
    _container.set_container_for_tests(bootstrap_for_tests(dispatcher=FakeTaskDispatcher()))

    pinned_now = datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return pinned_now

    monkeypatch.setattr(heartbeat_orchestration, "datetime", _FixedDatetime)
    monkeypatch.setattr("src.core.config.settings.VIDEO_ROOT_PATH", str(tmp_path))

    seed_db: Session = session_factory()
    try:
        now = pinned_now
        source = VideoSource(
            source_name="cam",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            last_validate_status="success",
            enabled=True,
            source_paused=False,
        )
        seed_db.add(source)
        seed_db.flush()
        seed_db.add(
            TaskLog(
                task_type=TaskType.SESSION_BUILD,
                task_target_id=1,
                status=TaskStatus.RUNNING,
                lease_owner="queue-expired-2",
                lease_expires_at=now - timedelta(hours=1),
                started_at=now - timedelta(hours=2),
                last_heartbeat_at=now - timedelta(hours=2),
            )
        )
        seed_db.add(
            TaskLog(
                task_type=TaskType.SESSION_BUILD,
                task_target_id=2,
                status=TaskStatus.PENDING,
                queue_task_id="orphan-task-id",
                detail_json={"scan_mode": "full"},
                created_at=now - timedelta(hours=2),
            )
        )
        seed_db.add(
            TaskLog(
                task_type=TaskType.SESSION_BUILD,
                task_target_id=99,
                status=TaskStatus.SUCCESS,
                created_at=now - timedelta(days=8),
                finished_at=now - timedelta(days=8),
            )
        )
        (tmp_path / "20260101").mkdir()
        (tmp_path / "20260101" / "present.mp4").write_bytes(b"present")
        seed_db.add(
            VideoFile(
                source_id=source.id,
                file_name="gone.mp4",
                file_path=str(tmp_path / "20260101" / "gone.mp4"),
                start_time=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
                duration_seconds=60,
            )
        )
        seed_db.commit()
    finally:
        seed_db.close()

    result = heartbeat_module.heartbeat()

    expected = {
        "dispatched_hot": 1,
        "timed_out": 1,
        "pending_recovered": 1,
        "logs_deleted": 0,
        "missing_marked": 0,
        "redelivered": 0,
    }
    actual = {k: result[k] for k in expected}
    assert actual == expected

    verify_db: Session = session_factory()
    try:
        persisted = (
            verify_db.query(AppRuntimeState).filter_by(state_key=HEARTBEAT_COUNTERS_KEY).one()
        )
        assert persisted.state_value == expected
    finally:
        verify_db.close()


def test_heartbeat_survives_failing_retention_and_still_marks_missing_files(
    pg_db_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing hourly retention sweep cannot poison the heartbeat (Issue 3).

    ``cleanup_old_task_logs`` is stubbed to delete a stale terminal row and
    then raise — the savepoint must roll the partial deletion back, the
    counter must stay at 0, ``mark_missing_video_files`` must still be
    attempted, the heartbeat must not raise, and the outer commit must
    still persist ``AppRuntimeState.heartbeat_last_counters`` (proof the
    outer transaction survived the savepoint rollback).
    """
    import src.tasks._task_maintenance_orchestration as heartbeat_orchestration
    import src.tasks.task_maintenance as heartbeat_module
    from src.db.metrics import HEARTBEAT_COUNTERS_KEY
    from src.models.app_runtime_state import AppRuntimeState

    session_factory = pg_db_factory

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(heartbeat_orchestration, "task_db_session", _task_session)
    _container.set_container_for_tests(bootstrap_for_tests(dispatcher=FakeTaskDispatcher()))

    pinned_now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    class _FixedClock:
        @staticmethod
        def now(tz: timezone | None = None) -> datetime:
            return pinned_now

    monkeypatch.setattr(heartbeat_orchestration, "datetime", _FixedClock)
    monkeypatch.setattr("src.core.config.settings.VIDEO_ROOT_PATH", str(tmp_path))

    seed_db: Session = session_factory()
    try:
        source = VideoSource(
            source_name="cam",
            camera_name="cam",
            location_name="home",
            source_type="local_directory",
            last_validate_status="success",
            enabled=True,
            source_paused=False,
        )
        seed_db.add(source)
        stale = TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=99,
            status=TaskStatus.SUCCESS,
            created_at=pinned_now - timedelta(days=8),
            finished_at=pinned_now - timedelta(days=8),
        )
        seed_db.add(stale)
        seed_db.commit()
        stale_id = stale.id
    finally:
        seed_db.close()

    def _failing_retention(db: Session, now: datetime) -> int:
        db.query(TaskLog).filter(TaskLog.id == stale_id).delete(synchronize_session=False)
        raise RuntimeError("simulated retention FK failure")

    missing_calls: list[int] = []

    def _counting_marker(db: Session) -> int:
        missing_calls.append(1)
        return 1

    monkeypatch.setattr(heartbeat_orchestration, "cleanup_old_task_logs", _failing_retention)
    monkeypatch.setattr(heartbeat_orchestration, "mark_missing_video_files", _counting_marker)

    result = heartbeat_module.heartbeat()

    assert result["logs_deleted"] == 0
    assert result["missing_marked"] == 1
    assert missing_calls == [1]

    verify_db: Session = session_factory()
    try:
        persisted = (
            verify_db.query(AppRuntimeState).filter_by(state_key=HEARTBEAT_COUNTERS_KEY).one()
        )
        assert persisted.state_value["logs_deleted"] == 0
        assert persisted.state_value["missing_marked"] == 1
        # The savepoint rollback restored the row the stub had deleted,
        # while the outer commit persisted the heartbeat counters.
        assert verify_db.query(TaskLog).filter_by(id=stale_id).one() is not None
    finally:
        verify_db.close()
