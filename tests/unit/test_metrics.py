"""Unit tests for the /metrics observability gauges.

These tests use a dedicated real PostgreSQL schema so the outbox lag,
task-recovery counters and checkpoint-progress queries from
:mod:`src.db.metrics` are pinned against a live database. Rows are
inserted via the ORM (with proper FK parent rows) on the dedicated
``postgres_real_engine``; the fixture truncates that schema at teardown
so each test starts from an empty database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from src.db.metrics import (
    checkpoint_progress,
    delivery_metrics,
    pending_backlog_metrics,
    task_recovery_metrics,
)
from src.models.app_runtime_state import AppRuntimeState
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_delivery import TaskDelivery
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource


@pytest.fixture
def engine(postgres_real_engine):
    yield postgres_real_engine
    _truncate_schema(postgres_real_engine)


def _truncate_schema(engine) -> None:
    from sqlalchemy import text

    from src.db.base_class import Base

    table_names = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    if table_names:
        with engine.connect() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
            conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert_delivery(engine, *, failed: bool, created_at: datetime) -> None:
    with Session(bind=engine) as session:
        task_log = TaskLog(task_type="src.tasks.x", status="success")
        session.add(task_log)
        session.flush()
        session.add(
            TaskDelivery(
                task_log_id=task_log.id,
                task_name="src.tasks.x",
                queue="celery",
                args_json=[],
                kwargs_json={},
                last_error="ConnectionError: broker down" if failed else None,
                created_at=created_at,
            )
        )
        session.commit()


def test_delivery_metrics_reports_lag_and_counts(engine) -> None:
    old_next = _now() - timedelta(seconds=600)
    recent_next = _now() - timedelta(seconds=10)
    _insert_delivery(engine, failed=False, created_at=old_next)
    _insert_delivery(engine, failed=False, created_at=recent_next)
    _insert_delivery(engine, failed=True, created_at=old_next)

    metrics = delivery_metrics(engine)
    assert metrics["pending_count"] == 3
    assert metrics["failed_count"] == 1
    assert metrics["oldest_pending_seconds"] is not None
    assert metrics["oldest_pending_seconds"] >= 599
    assert metrics["oldest_pending_seconds"] <= 600


def test_delivery_metrics_empty_pool_has_null_lag(engine) -> None:
    metrics = delivery_metrics(engine)
    assert metrics["pending_count"] == 0
    assert metrics["failed_count"] == 0
    assert metrics["oldest_pending_seconds"] is None


def test_task_recovery_metrics_counts_recovered_and_heartbeat(engine) -> None:
    with Session(bind=engine) as session:
        session.add(
            TaskLog(
                task_type="session_analysis", status="timeout", retry_count=0, recovery_attempt=2
            )
        )
        session.add(
            TaskLog(task_type="session_build", status="success", retry_count=0, recovery_attempt=0)
        )
        session.add(
            AppRuntimeState(
                state_key="heartbeat_last_counters",
                state_value={"timed_out": 3, "pending_recovered": 5},
            )
        )
        session.commit()

    metrics = task_recovery_metrics(engine)
    assert metrics["leases_recovered_total"] == 1
    assert metrics["last_heartbeat"]["leases_recovered"] == 3
    assert metrics["last_heartbeat"]["unleased_recovered"] == 5


def test_task_recovery_metrics_without_heartbeat_defaults_zero(engine) -> None:
    metrics = task_recovery_metrics(engine)
    assert metrics["leases_recovered_total"] == 0
    assert metrics["last_heartbeat"]["leases_recovered"] == 0
    assert metrics["last_heartbeat"]["unleased_recovered"] == 0


def test_checkpoint_progress_reports_running_analysis(engine) -> None:
    now = _now()
    with Session(bind=engine) as session:
        source = VideoSource(
            source_name="source-1",
            camera_name="客厅",
            location_name="客厅",
            source_type="local_directory",
            enabled=True,
        )
        session.add(source)
        session.flush()
        video_session = VideoSession(
            source_id=source.id,
            session_start_time=now,
            session_end_time=now,
            analysis_status="analyzing",
        )
        session.add(video_session)
        session.flush()
        video_session_id = video_session.id
        session.add(
            TaskLog(
                task_type="session_analysis",
                status="running",
                task_target_id=video_session_id,
                retry_count=0,
                recovery_attempt=0,
            )
        )
        for state, idx in (("success", 0), ("success", 1), ("pending", 2)):
            session.add(
                SessionAnalysisCheckpoint(
                    session_id=video_session_id,
                    analysis_run_id="run-1",
                    chunk_index=0,
                    sub_chunk_index=idx,
                    start_offset_seconds=0,
                    input_fingerprint="fp",
                    state=state,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    attempt_count=0,
                )
            )
        session.commit()

    progress = checkpoint_progress(engine)
    assert len(progress) == 1
    entry = progress[0]
    assert entry["session_id"] == video_session_id
    assert entry["completed_sub_chunks"] == 2
    assert entry["total_sub_chunks"] == 3


def test_pending_backlog_flags_stale_analysis(engine) -> None:
    """Analyzer rows past the grace are reported, never auto-recovered."""
    now = _now()
    with Session(bind=engine) as session:
        old_analysis = TaskLog(task_type="session_analysis", status="pending")
        old_analysis.created_at = now - timedelta(hours=2)
        fresh_analysis = TaskLog(task_type="session_analysis", status="pending")
        fresh_analysis.created_at = now - timedelta(seconds=60)
        fresh_build = TaskLog(task_type="session_build", status="pending")
        fresh_build.created_at = now - timedelta(seconds=100)
        session.add_all([old_analysis, fresh_analysis, fresh_build])
        session.commit()

    payload = pending_backlog_metrics(engine, now=now)

    assert payload["total_pending"] == 3
    assert payload["stale_pending"] == 1
    assert payload["oldest_pending_seconds"] is not None
    assert payload["oldest_pending_seconds"] >= 7199
    assert payload["thresholds_seconds"]["session_analysis"] == 1800
    assert payload["thresholds_seconds"]["default"] == 300

    by_type = {item["task_type"]: item for item in payload["by_task_type"]}
    assert by_type["session_analysis"]["pending_count"] == 2
    assert by_type["session_analysis"]["stale_count"] == 1
    assert by_type["session_analysis"]["stale"] is True
    assert by_type["session_build"]["stale"] is False
