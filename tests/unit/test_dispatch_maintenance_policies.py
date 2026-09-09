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

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeTaskDispatcher
from src.models.task_log import TaskLog
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
from src.services.maintenance.running_lease_recovery import recover_timed_out_tasks
from src.services.maintenance.unleased_recovery import recover_orphan_pending_tasks
from src.services.pipeline_constants import (
    ScanMode,
    TaskStatus,
    TaskType,
)
from src.tasks import _container

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
    monkeypatch.setattr("src.tasks._container.get_container", lambda: container)
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
    assert task_log.detail_json == {"scan_mode": "hot", "extra": "info"}


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
    finalize_cancelled_task_log(task_log, message="cancelled by user")
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
                enabled=enabled,
                source_paused=paused,
            )
        )
    db.commit()

    dispatcher = _bind_fake_dispatcher(monkeypatch)
    dispatched = dispatch_hot_builds(db)

    assert len(dispatched) == 2
    assert all(c.scan_mode == ScanMode.HOT for c in dispatcher.dispatched_session_build)


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
    assert recover_timed_out_tasks(db, now) == 1
    db.commit()
    db.refresh(task_log)
    assert task_log.status == TaskStatus.TIMEOUT

    # Idempotent: a second pass on the same now must do nothing.
    assert recover_timed_out_tasks(db, now) == 0


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
    assert recover_timed_out_tasks(db, now) == 0
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
    assert recover_orphan_pending_tasks(db, now) == 2


# ===========================================================================
# Maintenance — missing-file reconciliation policy
# ===========================================================================


# ===========================================================================
# Maintenance — heartbeat aggregator
# ===========================================================================


def test_heartbeat_aggregates_results_no_branching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``heartbeat`` aggregates deterministic counters from each policy module."""
    import src.tasks._task_maintenance_orchestration as heartbeat_orchestration
    import src.tasks.task_maintenance as heartbeat_module

    captured: dict[str, Any] = {}

    def fake_dispatch_hot_builds(db: Session) -> list[dict]:
        captured["dispatch_hot_builds"] = db
        return [{"source_id": 1, "task_id": "fake-1"}]

    def fake_recover_timed_out_tasks(db: Session, now: datetime) -> int:
        captured["recover_timed_out_tasks"] = (db, now)
        return 2

    def fake_recover_orphan_pending_tasks(db: Session, now: datetime) -> int:
        captured["recover_orphan_pending_tasks"] = (db, now)
        return 1

    def fake_cleanup_old_task_logs(db: Session, now: datetime) -> int:
        captured["cleanup_old_task_logs"] = (db, now)
        return 5

    def fake_mark_missing_video_files(db: Session) -> int:
        captured["mark_missing_video_files"] = db
        return 7

    monkeypatch.setattr(heartbeat_orchestration, "dispatch_hot_builds", fake_dispatch_hot_builds)
    monkeypatch.setattr(
        heartbeat_orchestration, "recover_timed_out_tasks", fake_recover_timed_out_tasks
    )
    monkeypatch.setattr(
        heartbeat_orchestration, "recover_orphan_pending_tasks", fake_recover_orphan_pending_tasks
    )
    monkeypatch.setattr(
        heartbeat_orchestration, "cleanup_old_task_logs", fake_cleanup_old_task_logs
    )
    monkeypatch.setattr(
        heartbeat_orchestration, "mark_missing_video_files", fake_mark_missing_video_files
    )

    mock_db = MagicMock()
    mock_task_db_session = MagicMock()
    mock_task_db_session.return_value.__enter__.return_value = mock_db
    mock_task_db_session.return_value.__exit__.return_value = False
    monkeypatch.setattr(heartbeat_orchestration, "task_db_session", mock_task_db_session)

    pinned_now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return pinned_now

    monkeypatch.setattr(heartbeat_orchestration, "datetime", _FixedDatetime)

    result = heartbeat_module.heartbeat()

    assert captured["dispatch_hot_builds"] is mock_db
    assert captured["recover_timed_out_tasks"][0] is mock_db
    assert captured["recover_orphan_pending_tasks"][0] is mock_db
    assert captured["cleanup_old_task_logs"][0] is mock_db
    assert captured["mark_missing_video_files"] is mock_db

    expected = {
        "dispatched_hot": 1,
        "timed_out": 2,
        "pending_recovered": 1,
        "logs_deleted": 5,
        "missing_marked": 7,
    }
    actual = {k: v for k, v in result.items() if k in expected}
    assert actual == expected


def test_heartbeat_skips_hourly_stages_outside_minute_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``heartbeat`` only runs cleanup + missing-file sweep at ``now.minute == 0``."""
    import src.tasks._task_maintenance_orchestration as heartbeat_orchestration
    import src.tasks.task_maintenance as heartbeat_module

    called: dict[str, int] = {
        "dispatch_hot_builds": 0,
        "recover_timed_out_tasks": 0,
        "recover_orphan_pending_tasks": 0,
        "cleanup_old_task_logs": 0,
        "mark_missing_video_files": 0,
    }

    def counter(name: str):
        def _fn(*args: Any, **kwargs: Any) -> Any:
            called[name] += 1
            return 0 if name != "dispatch_hot_builds" else []

        return _fn

    monkeypatch.setattr(
        heartbeat_orchestration, "dispatch_hot_builds", counter("dispatch_hot_builds")
    )
    monkeypatch.setattr(
        heartbeat_orchestration, "recover_timed_out_tasks", counter("recover_timed_out_tasks")
    )
    monkeypatch.setattr(
        heartbeat_orchestration,
        "recover_orphan_pending_tasks",
        counter("recover_orphan_pending_tasks"),
    )
    monkeypatch.setattr(
        heartbeat_orchestration, "cleanup_old_task_logs", counter("cleanup_old_task_logs")
    )
    monkeypatch.setattr(
        heartbeat_orchestration, "mark_missing_video_files", counter("mark_missing_video_files")
    )

    mock_db = MagicMock()
    mock_task_db_session = MagicMock()
    mock_task_db_session.return_value.__enter__.return_value = mock_db
    mock_task_db_session.return_value.__exit__.return_value = False
    monkeypatch.setattr(heartbeat_orchestration, "task_db_session", mock_task_db_session)

    pinned_now = datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return pinned_now

    monkeypatch.setattr(heartbeat_orchestration, "datetime", _FixedDatetime)
    heartbeat_module.heartbeat()
    assert called["dispatch_hot_builds"] == 1
    assert called["recover_timed_out_tasks"] == 1
    assert called["recover_orphan_pending_tasks"] == 1
    assert called["cleanup_old_task_logs"] == 0
    assert called["mark_missing_video_files"] == 0
