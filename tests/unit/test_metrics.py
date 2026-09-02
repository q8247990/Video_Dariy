"""Unit tests for the /metrics observability gauges.

These tests use an in-memory SQLite engine so the outbox lag, task
recovery counters and checkpoint-progress queries from
:mod:`src.db.metrics` are pinned without a live PostgreSQL. Rows are
inserted via raw SQL to stay independent of ORM FK hygiene.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import src.db.base  # noqa: F401  (registers all models on Base.metadata)
from src.db.base_class import Base
from src.db.metrics import (
    checkpoint_progress,
    outbox_metrics,
    task_recovery_metrics,
)


@pytest.fixture
def engine() -> Engine:
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return eng


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert_outbox(engine: Engine, status: str, next_attempt_at: str, task_log_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_event "
                "(event_id, task_log_id, task_name, queue, args_json, kwargs_json, "
                " status, attempt_count, next_attempt_at, created_at, updated_at) "
                "VALUES (:eid, :tl, 'src.tasks.x', 'celery', '[]', '{}', "
                " :status, 0, :next_attempt, :now, :now)"
            ),
            {
                "eid": str(uuid.uuid4()),
                "tl": task_log_id,
                "status": status,
                "next_attempt": next_attempt_at,
                "now": _now().isoformat(),
            },
        )


def test_outbox_metrics_reports_lag_and_counts(engine: Engine) -> None:
    old_next = (_now() - timedelta(seconds=600)).isoformat()
    recent_next = (_now() - timedelta(seconds=10)).isoformat()
    _insert_outbox(engine, "pending", old_next, task_log_id=1)
    _insert_outbox(engine, "pending", recent_next, task_log_id=2)
    _insert_outbox(engine, "failed", old_next, task_log_id=3)

    metrics = outbox_metrics(engine)
    assert metrics["pending_count"] == 2
    assert metrics["failed_count"] == 1
    assert metrics["publishing_count"] == 0
    assert metrics["oldest_pending_seconds"] is not None
    assert metrics["oldest_pending_seconds"] >= 599
    assert metrics["oldest_pending_seconds"] <= 600


def test_outbox_metrics_empty_pool_has_null_lag(engine: Engine) -> None:
    metrics = outbox_metrics(engine)
    assert metrics["pending_count"] == 0
    assert metrics["failed_count"] == 0
    assert metrics["oldest_pending_seconds"] is None


def test_task_recovery_metrics_counts_recovered_and_heartbeat(engine: Engine) -> None:
    now = _now()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task_log (task_type, status, retry_count, recovery_attempt, "
                " cancel_requested, created_at, updated_at) "
                "VALUES ('session_analysis', 'timeout', 0, 2, 0, :n, :n)"
            ),
            {"n": now.isoformat()},
        )
        conn.execute(
            text(
                "INSERT INTO task_log (task_type, status, retry_count, recovery_attempt, "
                " cancel_requested, created_at, updated_at) "
                "VALUES ('session_build', 'success', 0, 0, 0, :n, :n)"
            ),
            {"n": now.isoformat()},
        )
        conn.execute(
            text(
                "INSERT INTO app_runtime_state (state_key, state_value, created_at, updated_at) "
                "VALUES (:key, :val, :n, :n)"
            ),
            {
                "key": "heartbeat_last_counters",
                "val": '{"timed_out": 3, "pending_recovered": 5}',
                "n": now.isoformat(),
            },
        )

    metrics = task_recovery_metrics(engine)
    assert metrics["leases_recovered_total"] == 1
    assert metrics["last_heartbeat"]["leases_recovered"] == 3
    assert metrics["last_heartbeat"]["unleased_recovered"] == 5


def test_task_recovery_metrics_without_heartbeat_defaults_zero(engine: Engine) -> None:
    metrics = task_recovery_metrics(engine)
    assert metrics["leases_recovered_total"] == 0
    assert metrics["last_heartbeat"]["leases_recovered"] == 0
    assert metrics["last_heartbeat"]["unleased_recovered"] == 0


def test_checkpoint_progress_reports_running_analysis(engine: Engine) -> None:
    now = _now()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO video_session (id, source_id, session_start_time, "
                " session_end_time, analysis_status, created_at, updated_at) "
                "VALUES (1, 1, :s, :e, 'analyzing', :n, :n)"
            ),
            {"s": now.isoformat(), "e": now.isoformat(), "n": now.isoformat()},
        )
        conn.execute(
            text(
                "INSERT INTO task_log (task_type, status, task_target_id, "
                " retry_count, recovery_attempt, cancel_requested, created_at, updated_at) "
                "VALUES ('session_analysis', 'running', 1, 0, 0, 0, :n, :n)"
            ),
            {"n": now.isoformat()},
        )
        for state, idx in (("success", 0), ("success", 1), ("pending", 2)):
            conn.execute(
                text(
                    "INSERT INTO session_analysis_checkpoint "
                    "(session_id, analysis_run_id, chunk_index, sub_chunk_index, "
                    " start_offset_seconds, input_fingerprint, state, prompt_tokens, "
                    " completion_tokens, total_tokens, attempt_count, created_at, updated_at) "
                    "VALUES (1, 'run-1', 0, :idx, 0, 'fp', :state, 0, 0, 0, 0, :n, :n)"
                ),
                {"idx": idx, "state": state, "n": now.isoformat()},
            )

    progress = checkpoint_progress(engine)
    assert len(progress) == 1
    entry = progress[0]
    assert entry["session_id"] == 1
    assert entry["completed_sub_chunks"] == 2
    assert entry["total_sub_chunks"] == 3
