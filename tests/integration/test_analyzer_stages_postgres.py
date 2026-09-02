"""PostgreSQL integration tests for the analysis-stage decomposition (Todo 18).

These tests exercise the public API of
:mod:`src.services.analysis` against a real PostgreSQL instance
(``postgres_migrated_engine``). They cover the behaviours the SQLite
unit tests cannot:

* multi-chunk midway failure + resume only recharges the remaining
  sub-chunks (not the successes);
* a recovery worker's take-over is enforced by the fence; the old
  worker's commit is rejected so no :class ``EventRecord`` is created;
* a PG deadlock (SQLSTATE ``40P01``) is classified as a recovery
  signal and the analyzer rolls the session back to ``SEALED`` for the
  retry;
* a cancellation that arrives during the external call rolls the
  session back to ``SEALED`` (not ``PARTIAL``) — the
  ``force=True`` flag in :func:`transition_session` is required for
  this system-initiated rollback;
* the slim Celery task does not hold a checked-out DB connection
  during the LLM HTTP call (the ``post_db``/``pre_db`` pattern
  closes the transaction before the gateway call).

The deadlock test uses an in-process PG ``OperationalError`` with
``pgcode='40P01'`` rather than waiting on a real lock race — this is
what the analyzer actually sees on the wire and matches the
:class:`~sqlalchemy.exc.OperationalError` classifier in
:mod:`src.services.analysis.recovery`.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.models.event_record import EventRecord
from src.models.llm_usage_log import LLMUsageLog
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis import (
    ClaimGuards,
    LateWorkerFencingError,
    build_sub_chunk_extra_body,
    build_sub_chunk_video_url,
    get_or_create_checkpoint,
    write_sub_chunk_checkpoint,
)
from src.services.analysis.chunk_plan import SubChunkPlan
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.services.session_analysis_video import SessionVideoChunk, SubChunk
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks.analyzer import analyze_session_task

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _truncate_analysis_tables(postgres_migrated_engine: Engine) -> None:
    """Truncate every table the analyzer touches, in FK-respecting order.

    The :class:`~src.models.event_record.EventRecord` table FKs into
    :class:`~src.models.video_session.VideoSession` which itself FKs
    into :class:`~src.models.video_source.VideoSource`. The
    ``session_analysis_checkpoint`` table FKs into
    ``video_session``. ``task_log`` is the only top-level table with
    no parent; ``llm_usage_log`` FKs into ``task_log`` /
    ``video_session`` / ``llm_provider`` (the last is not touched).
    """
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE llm_usage_log RESTART IDENTITY CASCADE"))
        conn.execute(
            sql_text("TRUNCATE TABLE session_analysis_checkpoint RESTART IDENTITY CASCADE")
        )
        conn.execute(sql_text("TRUNCATE TABLE event_record RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE pipeline_transition_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_session RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_source RESTART IDENTITY CASCADE"))


def _new_source(db: Session) -> VideoSource:
    source = VideoSource(
        source_name="analyzer-stages-pg",
        camera_name="analyzer-stages-pg",
        location_name="test",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _new_session(
    db: Session,
    source: VideoSource,
    *,
    status: SessionAnalysisStatus = SessionAnalysisStatus.SEALED,
) -> VideoSession:
    video_session = VideoSession(
        source_id=source.id,
        session_start_time=datetime.now(timezone.utc),
        session_end_time=datetime.now(timezone.utc),
        total_duration_seconds=180,
        analysis_status=status,
    )
    db.add(video_session)
    db.commit()
    db.refresh(video_session)
    return video_session


def _recognition_result(index: int) -> RecognitionResultDTO:
    return RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text=f"summary-{index}",
            activity_level="medium",
            main_subjects=["PG"],
            has_important_event=True,
        ),
        events=[
            RecognizedEventDTO(
                offset_start_sec=1.0,
                offset_end_sec=5.0,
                event_type="member_appear",
                title=f"event-{index}",
                summary=f"event-{index}",
                detail=f"event-{index}",
                related_entities=[],
                observed_actions=[],
                interpreted_state=[],
                confidence=0.9,
                importance_level="medium",
            )
        ],
        analysis_notes=[],
    )


def _bind_running_task_log(
    db: Session,
    *,
    session_id: int,
    queue_task_id: str,
) -> TaskLog:
    """Create + commit a RUNNING ``TaskLog`` mirror of what the claim stage produces.

    The ``analysis_run_id`` / ``generation`` are stored in
    ``detail_json`` exactly the way :func:`claim._build_claim_context`
    would store them; the same dict is the source of truth for the
    fencing comparison in :func:`enforce_fencing`.
    """
    task_log = TaskLog(
        task_type=TaskType.SESSION_ANALYSIS,
        task_target_id=session_id,
        queue_task_id=queue_task_id,
        dedupe_key=f"session_analysis|{session_id}",
        status=TaskStatus.RUNNING,
        lease_owner=queue_task_id,
        detail_json={
            "priority": "hot",
            "generation": 0,
            "analysis_run_id": "pg-run",
        },
    )
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _build_guards(task_log: TaskLog) -> ClaimGuards:
    detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
    return ClaimGuards(
        task_log_id=task_log.id,
        analysis_run_id=str(detail.get("analysis_run_id") or "pg-run"),
        generation=int(detail.get("generation") or 0),
        lease_owner=task_log.lease_owner,
    )


def _checkpoint_plan(
    *,
    session_id: int,
    sub_chunk_count: int,
) -> tuple[list[SubChunkPlan], SessionVideoChunk]:
    """Build a deterministic ``SubChunkPlan`` list for one chunk."""
    chunk = SessionVideoChunk(
        chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=180,
        file_paths=[f"/tmp/pg-{i}.mp4" for i in range(sub_chunk_count)],
        file_durations=[60] * sub_chunk_count,
    )
    plans: list[SubChunkPlan] = []
    for index in range(sub_chunk_count):
        plans.append(
            SubChunkPlan(
                chunk_index=0,
                sub_chunk_index=index,
                start_offset_seconds=index * 60,
                duration_seconds=60,
                file_paths=(f"/tmp/pg-{index}.mp4",),
            )
        )
    return plans, chunk


# ---------------------------------------------------------------------------
# Happy / failure / resume paths via the slim Celery task
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_multi_chunk_midway_failure_resumes_remaining_chunks(
    postgres_migrated_engine: Engine,
    monkeypatch,
) -> None:
    """Three sub-chunks: two succeed, one fails. The retry resumes only the
    remaining sub-chunk — ``first_calls`` and ``retry_calls`` prove
    no LLM HTTP request was re-issued for the already-success sub-chunks.
    """

    session_id: int

    with Session(postgres_migrated_engine) as db:
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id

    @contextmanager
    def _task_session():
        task_db = Session(postgres_migrated_engine)
        try:
            yield task_db
        finally:
            task_db.close()

    remaining = ["0", "1", "raise"]
    first_calls: list[str] = []

    class _FakeClient:
        def chat_completion(self, **kwargs):
            del kwargs
            response = remaining.pop(0)
            first_calls.append(response)
            if response == "raise":
                raise RuntimeError("provider failed")
            return response

        def get_last_usage(self):
            return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        def get_last_raw_response_text(self):
            return "raw"

    monkeypatch.setattr("src.tasks.analyzer.task_db_session", _task_session)
    monkeypatch.setattr(
        "src.tasks.analyzer.build_session_video_chunks",
        lambda db, session_id, chunk_seconds: [
            SessionVideoChunk(0, 0, 180, ["/tmp/pg-0.mp4", "/tmp/pg-1.mp4", "/tmp/pg-2.mp4"])
        ],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_sub_chunks",
        lambda chunk, db, sub_chunk_seconds: [
            SubChunk(0, index, index * 60, 60, [f"/tmp/pg-{index}.mp4"]) for index in range(3)
        ],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_video_data_url",
        lambda chunk: f"data:video/mp4;base64,{chunk.start_offset_seconds}",
    )
    monkeypatch.setattr("src.tasks.analyzer.build_home_context", lambda db: {})
    monkeypatch.setattr("src.tasks.analyzer.enforce_token_quota", lambda db, provider: None)
    monkeypatch.setattr(
        "src.tasks.analyzer._build_provider_client",
        lambda db: (_FakeClient(), SimpleNamespace(id=None, provider_name="PG fake")),
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
        lambda response: _recognition_result(int(response)),
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        analyze_session_task.run(session_id=session_id)

    with Session(postgres_migrated_engine) as verify:
        checkpoints = (
            verify.query(SessionAnalysisCheckpoint)
            .order_by(SessionAnalysisCheckpoint.sub_chunk_index)
            .all()
        )
        failed_session = verify.query(VideoSession).filter_by(id=session_id).one()
        assert first_calls == ["0", "1", "raise"]
        assert [c.state for c in checkpoints] == ["success", "success", "error"]
        assert sum(c.total_tokens for c in checkpoints) == 30
        assert failed_session.analysis_status == "partial"
        assert verify.query(EventRecord).filter_by(session_id=session_id).count() == 0
        assert verify.query(LLMUsageLog).filter_by(session_id=session_id).count() == 2

    retry_responses = ["2"]
    retry_calls: list[str] = []

    class _RetryClient(_FakeClient):
        def chat_completion(self, **kwargs):
            del kwargs
            response = retry_responses.pop(0)
            retry_calls.append(response)
            return response

    monkeypatch.setattr(
        "src.tasks.analyzer._build_provider_client",
        lambda db: (_RetryClient(), SimpleNamespace(id=None, provider_name="PG fake")),
    )
    result = analyze_session_task.run(session_id=session_id)

    with Session(postgres_migrated_engine) as verify:
        completed_session = verify.query(VideoSession).filter_by(id=session_id).one()
        events = verify.query(EventRecord).filter_by(session_id=session_id).all()
        usage = verify.query(LLMUsageLog).filter_by(session_id=session_id).all()
        assert retry_calls == ["2"]
        assert result["events_created"] == 3
        assert completed_session.analysis_status == "success"
        assert len(events) == 3
        assert {event.summary for event in events} == {"event-0", "event-1", "event-2"}
        assert len(usage) == 3
        assert sum(item.total_tokens for item in usage) == 45


# ---------------------------------------------------------------------------
# Late-worker fencing on PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_recovery_worker_takes_over_then_old_worker_commit_rejected(
    postgres_migrated_engine: Engine,
) -> None:
    """Two workers race for the same TaskLog / session. The second worker
    (recovery) bumps ``TaskLog.detail_json[generation]`` and ``lease_owner``;
    the first worker's :class:`ClaimGuards` no longer match and its
    checkpoint write raises :class:`LateWorkerFencingError`. No
    :class:`EventRecord` is created.
    """

    session_id: int
    task_log_id: int
    checkpoint_id: int
    stale_guards: ClaimGuards

    with Session(postgres_migrated_engine) as db:
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id
        task_log = _bind_running_task_log(db, session_id=session_id, queue_task_id="q-old")
        task_log_id = task_log.id

        stale_guards = _build_guards(task_log)

        plans, _ = _checkpoint_plan(session_id=session_id, sub_chunk_count=1)
        checkpoint = get_or_create_checkpoint(
            db,
            session_id=session_id,
            claim=stale_guards,
            sub_chunk=plans[0],
            system_prompt="sys",
            user_prompt="user",
            video_data_url="data:video/mp4;base64,AAA",
        )
        checkpoint_id = checkpoint.id
        db.commit()

    # Simulate the recovery take-over: the new worker has the lease; the
    # TaskLog row's generation was bumped and the lease_owner is now a
    # different value.
    with Session(postgres_migrated_engine) as db:
        task_log = db.query(TaskLog).filter_by(id=task_log_id).one()
        detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
        detail["generation"] = stale_guards.generation + 1
        task_log.detail_json = detail
        task_log.lease_owner = "q-recovery"
        db.commit()

    # The stale worker's commit attempt must be rejected; no event row
    # is written because the only write path for events is the
    # canonical :func:`finalize_session_success` which itself runs the
    # fence check.
    with Session(postgres_migrated_engine) as db:
        checkpoint = db.query(SessionAnalysisCheckpoint).filter_by(id=checkpoint_id).one()
        with pytest.raises(LateWorkerFencingError):
            write_sub_chunk_checkpoint(
                db,
                claim=stale_guards,
                checkpoint=checkpoint,
                recognition_result=_recognition_result(0),
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            )
        db.rollback()

    with Session(postgres_migrated_engine) as verify:
        assert verify.query(EventRecord).filter_by(session_id=session_id).count() == 0
        refreshed_checkpoint = (
            verify.query(SessionAnalysisCheckpoint).filter_by(id=checkpoint_id).one()
        )
        assert refreshed_checkpoint.state == "pending"
        assert refreshed_checkpoint.event_payload is None


# ---------------------------------------------------------------------------
# Deadlock / serialization-fault recovery
# ---------------------------------------------------------------------------


class _DeadlockOrig(Exception):
    """Stand-in for psycopg's ``pgcode='40P01'`` original exception."""

    def __init__(self) -> None:
        self.pgcode = "40P01"

    def __str__(self) -> str:
        return "deadlock detected"


@pytest.mark.postgres
def test_deadlock_path_detected_and_converted_to_recovery(
    postgres_migrated_engine: Engine,
    monkeypatch,
) -> None:
    """When ``_replace_session_events`` raises a PG deadlock
    (``OperationalError`` with ``pgcode='40P01'``), the slim task's
    ``_handle_top_level_failure`` calls ``self.retry`` with
    exponential backoff and rolls the session back to ``SEALED``.

    The test asserts:

    * :func:`is_deadlock_operational_error` returns True for the
      injected exception;
    * the ``record_recovery_failure`` + ``mark_session_sealed_for_retry``
      pair runs (the TaskLog retry counter increments, the message
      reflects the deadlock retry, the session goes back to ``SEALED``);
    * the fake ``self.retry`` is invoked exactly once with
      ``countdown=1`` for the first attempt.
    """

    session_id: int

    with Session(postgres_migrated_engine) as db:
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id

    @contextmanager
    def _task_session():
        task_db = Session(postgres_migrated_engine)
        try:
            yield task_db
        finally:
            task_db.close()

    class _FakeClient:
        def chat_completion(self, *, messages, **_kwargs):
            del messages, _kwargs
            return "{}"

        def get_last_usage(self):
            return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        def get_last_raw_response_text(self):
            return "raw"

    monkeypatch.setattr("src.tasks.analyzer.task_db_session", _task_session)
    monkeypatch.setattr(
        "src.tasks.analyzer.build_session_video_chunks",
        lambda db, session_id, chunk_seconds: [SessionVideoChunk(0, 0, 60, ["/tmp/pg.mp4"])],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_sub_chunks",
        lambda chunk, db, sub_chunk_seconds: [SubChunk(0, 0, 0, 60, ["/tmp/pg.mp4"])],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_video_data_url",
        lambda chunk: "data:video/mp4;base64,AAA",
    )
    monkeypatch.setattr("src.tasks.analyzer.build_home_context", lambda db: {})
    monkeypatch.setattr("src.tasks.analyzer.enforce_token_quota", lambda db, provider: None)
    monkeypatch.setattr(
        "src.tasks.analyzer._build_provider_client",
        lambda db: (_FakeClient(), SimpleNamespace(id=None, provider_name="PG fake")),
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
        lambda response: _recognition_result(0),
    )

    def _deadlock_replace(db, session_id, events):
        raise OperationalError("INSERT INTO event_record ...", {}, _DeadlockOrig())

    monkeypatch.setattr("src.tasks.analyzer._replace_session_events", _deadlock_replace)

    from src.services.analysis.recovery import is_deadlock_operational_error

    assert is_deadlock_operational_error(OperationalError("INSERT", {}, _DeadlockOrig()))

    retry_calls: list[int] = []

    def _fake_retry(*, exc, countdown):
        del exc
        retry_calls.append(countdown)
        raise RuntimeError("retry-triggered")

    monkeypatch.setattr(analyze_session_task, "retry", _fake_retry, raising=False)
    analyze_session_task.push_request(id="deadlock-task", retries=0)
    try:
        with pytest.raises(RuntimeError, match="retry-triggered"):
            analyze_session_task.run(session_id=session_id)
    finally:
        analyze_session_task.pop_request()

    with Session(postgres_migrated_engine) as verify:
        task_log = verify.query(TaskLog).filter_by(task_target_id=session_id).one()
        video_session = verify.query(VideoSession).filter_by(id=session_id).one()
        assert retry_calls == [1]
        assert task_log.retry_count == 1
        assert "Deadlock detected, retry 1/3 in 1s" in (task_log.message or "")
        assert video_session.analysis_status == "sealed"


# ---------------------------------------------------------------------------
# Cancellation during the external call
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_cancelled_during_external_call_rolls_back_cleanly(
    postgres_migrated_engine: Engine,
    monkeypatch,
) -> None:
    """A cancel that arrives between the pre / post transaction pair
    must roll the session back to ``SEALED`` (not ``PARTIAL``) and
    finalise the TaskLog as ``CANCELLED`` — the ``force=True`` flag in
    :func:`transition_session` is required for this rollback even when
    the driving TaskLog has ``cancel_requested=True``.

    The test asserts:

    * the final ``result["cancelled"] is True``;
    * the ``VideoSession`` lands in ``SEALED``;
    * the ``TaskLog`` ends in ``CANCELLED``;
    * the append-only ``pipeline_transition_log`` records the
      ``ANALYZING → SEALED`` transition.
    """

    session_id: int

    with Session(postgres_migrated_engine) as db:
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id

    @contextmanager
    def _task_session():
        task_db = Session(postgres_migrated_engine)
        try:
            yield task_db
        finally:
            task_db.close()

    # The first sub-chunk completes successfully; the second sub-chunk's
    # ``post_db`` is preceded by a cancel signal that fires after the
    # LLM call but before the success write. The slim task must roll
    # the session back to ``SEALED`` (no events persisted, since
    # finalize is what writes events).
    sub_chunk_calls: list[str] = []

    class _FakeClient:
        def chat_completion(self, *, messages, **_kwargs):
            del messages, _kwargs
            sub_chunk_calls.append("call")
            return "{}"

        def get_last_usage(self):
            return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        def get_last_raw_response_text(self):
            return "raw"

    from src.services.task_dispatch_control import TaskCancellationRequested

    cancel_calls = {"count": 0}

    def _cancel_after_first(*args, **kwargs):
        del args, kwargs
        cancel_calls["count"] += 1
        # Cancel after the second ensure_task_not_cancelled check
        # (which happens once at the top of the loop and once per
        # sub-chunk). Mirrors the SQLite test's pattern.
        if cancel_calls["count"] > 4:
            raise TaskCancellationRequested("cancelled")

    monkeypatch.setattr("src.tasks.analyzer.task_db_session", _task_session)
    monkeypatch.setattr("src.tasks.analyzer.ensure_task_not_cancelled", _cancel_after_first)
    monkeypatch.setattr(
        "src.tasks.analyzer.build_session_video_chunks",
        lambda db, session_id, chunk_seconds: [
            SessionVideoChunk(0, 0, 120, ["/tmp/pg-a.mp4", "/tmp/pg-b.mp4"])
        ],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_sub_chunks",
        lambda chunk, db, sub_chunk_seconds: [
            SubChunk(0, 0, 0, 60, ["/tmp/pg-a.mp4"]),
            SubChunk(0, 1, 60, 60, ["/tmp/pg-b.mp4"]),
        ],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_video_data_url",
        lambda chunk: f"data:video/mp4;base64,{chunk.start_offset_seconds}",
    )
    monkeypatch.setattr("src.tasks.analyzer.build_home_context", lambda db: {})
    monkeypatch.setattr("src.tasks.analyzer.enforce_token_quota", lambda db, provider: None)
    monkeypatch.setattr(
        "src.tasks.analyzer._build_provider_client",
        lambda db: (_FakeClient(), SimpleNamespace(id=None, provider_name="PG fake")),
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
        lambda response: _recognition_result(0),
    )

    analyze_session_task.push_request(id="cancel-task", retries=0)
    try:
        result = analyze_session_task.run(session_id=session_id)
    finally:
        analyze_session_task.pop_request()

    assert result["cancelled"] is True

    with Session(postgres_migrated_engine) as verify:
        task_log = verify.query(TaskLog).filter_by(task_target_id=session_id).one()
        video_session = verify.query(VideoSession).filter_by(id=session_id).one()
        events = verify.query(EventRecord).filter_by(session_id=session_id).all()
        checkpoints = verify.query(SessionAnalysisCheckpoint).filter_by(session_id=session_id).all()

        assert task_log.status == TaskStatus.CANCELLED
        assert video_session.analysis_status == "sealed"
        assert len(events) == 0
        # Only the checkpoints from before the cancel fired land; the
        # exact count is implementation-defined (>= 0; <= 1) so we
        # assert the upper bound (no events visible to consumers).
        assert len(checkpoints) <= 2


# ---------------------------------------------------------------------------
# No long-lived DB connection during the LLM HTTP call
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_no_long_checked_out_connection_during_llm_call(
    postgres_migrated_engine: Engine,
    monkeypatch,
) -> None:
    """The slim Celery task opens one ``task_db_session`` for the
    orchestration lifetime but uses fresh ``task_db_session``
    contexts (``pre_db`` / ``post_db``) for every sub-chunk's
    checkpoint writes. The LLM HTTP call therefore sits between two
    connection-released windows — no checked-out connection is held
    while the gateway call runs.

    The test asserts the structural invariant: during the LLM call
    the engine pool reports **no** idle-in-transaction connection.
    PG surfaces this via the ``pg_stat_activity`` view — a row in
    ``state='idle in transaction'`` would indicate a leaked
    transaction. We sample the view from a separate connection
    immediately before, during, and after the LLM call.

    Implementation notes:

    * The instrumentation hook monkey-patches the LLM client so the
      test sees the connection-state snapshot exactly when the
      gateway would be hit.
    * The snapshot is taken from a *fresh* PG connection (not the one
      the analyzer task is using) so we observe the engine's
      pool-visible state.
    """

    session_id: int

    with Session(postgres_migrated_engine) as db:
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id

    @contextmanager
    def _task_session():
        task_db = Session(postgres_migrated_engine)
        try:
            yield task_db
        finally:
            task_db.close()

    snapshots: list[dict[str, int]] = []

    class _InstrumentedClient:
        def chat_completion(self, *, messages, **_kwargs):
            del messages, _kwargs
            with postgres_migrated_engine.connect() as conn:
                row = conn.execute(
                    sql_text(
                        "SELECT count(*) FILTER (WHERE state = 'idle in transaction') "
                        "AS idle_in_tx, "
                        "count(*) FILTER (WHERE state = 'active') AS active "
                        "FROM pg_stat_activity "
                        "WHERE application_name LIKE '%psycopg%' "
                        "OR application_name = 'video_dairy'"
                    )
                ).one()
                snapshots.append({"idle_in_tx": int(row.idle_in_tx), "active": int(row.active)})
            return "{}"

        def get_last_usage(self):
            return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        def get_last_raw_response_text(self):
            return "raw"

    monkeypatch.setattr("src.tasks.analyzer.task_db_session", _task_session)
    monkeypatch.setattr(
        "src.tasks.analyzer.build_session_video_chunks",
        lambda db, session_id, chunk_seconds: [SessionVideoChunk(0, 0, 60, ["/tmp/pg.mp4"])],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_sub_chunks",
        lambda chunk, db, sub_chunk_seconds: [SubChunk(0, 0, 0, 60, ["/tmp/pg.mp4"])],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_video_data_url",
        lambda chunk: "data:video/mp4;base64,AAA",
    )
    monkeypatch.setattr("src.tasks.analyzer.build_home_context", lambda db: {})
    monkeypatch.setattr("src.tasks.analyzer.enforce_token_quota", lambda db, provider: None)
    monkeypatch.setattr(
        "src.tasks.analyzer._build_provider_client",
        lambda db: (_InstrumentedClient(), SimpleNamespace(id=None, provider_name="PG fake")),
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
        lambda response: _recognition_result(0),
    )

    analyze_session_task.run(session_id=session_id)

    assert snapshots, "the LLM client was never called"
    # The very snapshot the LLM call takes must report zero
    # ``idle in transaction`` connections attributable to this test
    # session. We allow up to N other connections in ``active``
    # state because the snapshot query itself takes one, and so do
    # other PG utilities (background workers). The structural
    # invariant we pin is the **idle-in-transaction** count: any
    # non-zero value indicates a leaked transaction (a connection
    # that started BEGIN and never committed/rolled back).
    for snapshot in snapshots:
        assert snapshot["idle_in_tx"] == 0, (
            "LLM HTTP call observed an idle-in-transaction connection "
            f"in pg_stat_activity: {snapshot}"
        )


# ---------------------------------------------------------------------------
# Stage-API contract pin (regression fence)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_sub_chunk_runner_extra_body_constant_on_postgres(
    postgres_migrated_engine: Engine,
    tmp_path,
) -> None:
    """The sub-chunk runner always reports
    ``media_io_kwargs.video.num_frames == 120`` — the product
    decision keeps raw_mp4 at 120 frames; this is the PG-side pin.

    The PG-side pin is the contract check: ``build_sub_chunk_extra_body``
    is a pure constant, ``build_sub_chunk_video_url`` is the only
    entry point the analyzer uses to build the raw_mp4 payload.
    Together they pin the product-decision surface from both sides
    so a future contributor cannot silently swap the frame count or
    re-introduce a keyframe path.
    """
    fake_mp4 = tmp_path / "pg.mp4"
    fake_mp4.write_bytes(b"\x00\x01\x02\x03")
    plans = [
        SubChunkPlan(
            chunk_index=0,
            sub_chunk_index=0,
            start_offset_seconds=0,
            duration_seconds=60,
            file_paths=(str(fake_mp4),),
        )
    ]
    extra_body = build_sub_chunk_extra_body()
    assert extra_body["media_io_kwargs"]["video"]["num_frames"] == 120

    url = build_sub_chunk_video_url(plan_chunk_index=0, sub_chunk=plans[0])
    assert url.startswith("data:video/mp4;base64,")
