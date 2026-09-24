"""PostgreSQL integration tests for the analyzer pipeline (Todo "测试边界收敛" first wave).

These tests exercise the public :func:`analyze_session_task` Celery
entry against a real PostgreSQL instance (``postgres_migrated_engine``)
and a scripted
:class:`~src.application.ports.llm_gateway.LLMGatewayFactoryPort`
injected through the task-layer container holder
(:func:`src.tasks._container.set_container_for_tests`). The analyzer
pipeline's media-layout helpers are wired through
:class:`~src.application.bootstrap_fakes.FakeAnalysisPorts`.

The only allowed monkey-patches against ``src`` internals are:

* :func:`src.tasks._analyzer_orchestration.task_db_session` (DB-env
  seam) — replaced once per test with a context manager that opens a
  Session against the migrated engine;
* ``analyze_session_task.retry`` (Celery runtime port for deadlock
  injection);
* ``outbox_contracts._reset_emitted_event_ids_for_testing`` (authorized
  test hook — listed for completeness even though no test in this file
  uses it).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import (
    FakeAnalysisPorts,
    ScriptedVisionGateway,
    ScriptedVisionGatewayFactory,
)
from src.models.event_record import EventRecord
from src.models.llm_provider import LLMProvider
from src.models.llm_usage_log import LLMUsageLog
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis import (
    LateWorkerFencingError,
    build_sub_chunk_extra_body,
    build_sub_chunk_video_url,
    write_sub_chunk_checkpoint,
)
from src.services.analysis.chunk_plan import SubChunkPlan
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks import _analyzer_orchestration, _container  # noqa: F401
from src.tasks.analyzer import analyze_session_task

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _truncate_analysis_tables(postgres_migrated_engine: Engine) -> None:
    """Truncate every table the analyzer touches, in FK-respecting order."""

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
        conn.execute(sql_text("TRUNCATE TABLE llm_provider RESTART IDENTITY CASCADE"))


@pytest.fixture
def analyzer_db(postgres_migrated_engine: Engine) -> Iterator[Session]:
    """Function-scoped session bound to the migrated engine for the
    base-action wiring.
    """

    db = Session(bind=postgres_migrated_engine)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _base_action(
    analyzer_db: Session,
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Wire the task-layer container + ports holder for every test.

    Installs a scripted vision factory with an empty-responses gateway
    and a 1-chunk × 3-sub-chunk :class:`FakeAnalysisPorts`. The
    ``task_db_session`` is replaced with a context manager that opens
    Sessions against the migrated engine so the analyzer task's
    connections see the seeded rows.

    Tests customise either the gateway responses (via
    :meth:`ScriptedVisionGateway.queue_responses`) or the ports
    (via :func:`_container.set_analysis_ports_for_tests`) before
    calling :func:`analyze_session_task.run`.
    """

    factory = ScriptedVisionGatewayFactory()
    factory.install_gateway(ScriptedVisionGateway(responses=[]))
    container = bootstrap_for_tests(llm_factory=factory)
    _container.set_container_for_tests(container)
    _container.set_analysis_ports_for_tests(FakeAnalysisPorts())

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = Session(bind=postgres_migrated_engine)
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr("src.tasks._analyzer_orchestration.task_db_session", _task_session)
    try:
        yield
    finally:
        _container.reset_container_for_tests()
        _container.reset_analysis_ports_for_tests()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _new_source(
    db: Session,
    *,
    source_name: str = "analyzer-stages-pg",
) -> VideoSource:
    source = VideoSource(
        source_name=source_name,
        camera_name=source_name,
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


def _seed_provider(db: Session) -> LLMProvider:
    """Seed a default-vision :class:`LLMProvider` for the analyzer task."""

    provider = LLMProvider(
        provider_name="analyzer-stages-pg",
        provider_type="vision_provider",
        api_base_url="http://example.invalid/v1",
        api_key="",
        model_name="vision-test",
        timeout_seconds=30,
        enabled=True,
        supports_vision=True,
        is_default_vision=True,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def _recognition_result(index: int) -> RecognitionResultDTO:
    return RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text=f"summary-{index}",
            activity_level="medium",
            main_subjects=["PG"],
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
            )
        ],
        analysis_notes=[],
    )


def _recognition_result_json(index: int) -> str:
    return _recognition_result(index).model_dump_json()


def _latest_gateway() -> ScriptedVisionGateway:
    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    return factory.gateways[-1]


def _set_responses(*responses: str) -> None:
    """Queue responses on the gateway the next run will consume.

    Preserves the existing gateway's ``on_call`` side-effect hook
    (needed by the cancel test); falls back to installing a fresh
    gateway when no installed gateway exists.
    """

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    if factory._gateway is not None:
        factory._gateway.queue_responses(list(responses))
    else:
        factory.install_gateway(ScriptedVisionGateway(responses=list(responses)))


# ---------------------------------------------------------------------------
# Happy / failure / resume paths via the slim Celery task
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_multi_chunk_midway_failure_resumes_remaining_chunks(
    postgres_migrated_engine: Engine,
) -> None:
    """Three sub-chunks: two succeed, one fails. The retry resumes only the
    remaining sub-chunk — the gateway's call log proves no LLM HTTP
    request was re-issued for the already-success sub-chunks.
    """

    with Session(postgres_migrated_engine) as db:
        _seed_provider(db)
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id

    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        "raise",
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
        first_call_count = len(_latest_gateway().calls)
        assert first_call_count == 3
        assert [c.state for c in checkpoints] == ["success", "success", "error"]
        assert sum(c.total_tokens for c in checkpoints) == 30
        assert failed_session.analysis_status == "partial"
        assert verify.query(EventRecord).filter_by(session_id=session_id).count() == 0
        assert verify.query(LLMUsageLog).filter_by(session_id=session_id).count() == 2

    _set_responses(_recognition_result_json(2))
    result = analyze_session_task.run(session_id=session_id)

    with Session(postgres_migrated_engine) as verify:
        completed_session = verify.query(VideoSession).filter_by(id=session_id).one()
        events = verify.query(EventRecord).filter_by(session_id=session_id).all()
        usage = verify.query(LLMUsageLog).filter_by(session_id=session_id).all()
        retry_call_count = len(_latest_gateway().calls)
        assert retry_call_count == 1
        assert result["events_created"] == 3
        assert completed_session.analysis_status == "success"
        assert len(events) == 3
        assert {event.summary for event in events} == {"event-0", "event-1", "event-2"}
        assert len(usage) == 3
        assert sum(item.total_tokens for item in usage) == 45


# ---------------------------------------------------------------------------
# Late-worker fencing on PostgreSQL
# ---------------------------------------------------------------------------


def _build_guards(task_log: TaskLog) -> Any:
    """Build a :class:`ClaimGuards` from a real TaskLog row's detail_json.

    Kept narrow: the integration test that drives the fencing scenario
    is allowed to construct guards from a DB row because that is the
    public shape of a claim context (``detail_json["analysis_run_id"]``
    / ``detail_json["generation"]`` are the fencing fields the claim
    stage writes). The test then exercises
    :func:`write_sub_chunk_checkpoint` directly because that is the
    single write path the slim task uses to land success rows.
    """
    from src.services.analysis.checkpoint_writer import ClaimGuards

    detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
    return ClaimGuards(
        task_log_id=task_log.id,
        analysis_run_id=str(detail.get("analysis_run_id") or "pg-run"),
        generation=int(detail.get("generation") or 0),
        lease_owner=task_log.lease_owner,
    )


def _bind_running_task_log(
    db: Session,
    *,
    session_id: int,
    queue_task_id: str,
) -> TaskLog:
    """Create + commit a RUNNING TaskLog mirror of what the claim stage produces."""

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


@pytest.mark.postgres
def test_recovery_worker_takes_over_then_old_worker_commit_rejected(
    postgres_migrated_engine: Engine,
) -> None:
    """A first worker hands off the session to a recovery worker (lease
    owner + generation bumped). The first worker's claim context no
    longer matches the current TaskLog row, so any
    :func:`write_sub_chunk_checkpoint` against the seeded checkpoint
    raises :class:`LateWorkerFencingError` and no
    :class:`EventRecord` is created. The test seeds the TaskLog + claim
    context directly because the recovery take-over scenario is a
    specific fencing shape that the public claim path cannot produce in
    a single run; the test stays at the TaskLog / checkpoint boundary
    rather than reaching into private claim internals.
    """

    session_id: int
    task_log_id: int
    checkpoint_id: int

    with Session(postgres_migrated_engine) as db:
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id
        task_log = _bind_running_task_log(db, session_id=session_id, queue_task_id="q-old")
        task_log_id = task_log.id

        stale_guards = _build_guards(task_log)

        plans = [
            SubChunkPlan(
                chunk_index=0,
                sub_chunk_index=0,
                start_offset_seconds=0,
                duration_seconds=60,
                file_paths=("/tmp/pg-0.mp4",),
            )
        ]
        checkpoint = _build_initial_checkpoint(
            db,
            session_id=session_id,
            guards=stale_guards,
            plan=plans[0],
        )
        checkpoint_id = checkpoint.id
        db.commit()

    # Recovery take-over: the new worker has the lease; the TaskLog
    # row's generation was bumped and the lease_owner is now a
    # different value.
    with Session(postgres_migrated_engine) as db:
        task_log = db.query(TaskLog).filter_by(id=task_log_id).one()
        detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
        detail["generation"] = stale_guards.generation + 1
        task_log.detail_json = detail
        task_log.lease_owner = "q-recovery"
        db.commit()

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


def _build_initial_checkpoint(
    db: Session,
    *,
    session_id: int,
    guards: Any,
    plan: SubChunkPlan,
) -> SessionAnalysisCheckpoint:
    """Insert a ``pending`` checkpoint so the fencing test has a row to
    attempt the success write against.
    """

    checkpoint = SessionAnalysisCheckpoint(
        session_id=session_id,
        analysis_run_id=guards.analysis_run_id,
        chunk_index=plan.chunk_index,
        sub_chunk_index=plan.sub_chunk_index,
        start_offset_seconds=plan.start_offset_seconds,
        input_fingerprint="seed",
        state="pending",
    )
    db.add(checkpoint)
    db.flush()
    return checkpoint


# ---------------------------------------------------------------------------
# Deadlock / serialization-fault recovery
# ---------------------------------------------------------------------------


class _DeadlockOrig(Exception):
    def __init__(self) -> None:
        self.pgcode = "40P01"

    def __str__(self) -> str:
        return "deadlock detected"


@pytest.mark.postgres
def test_deadlock_path_detected_and_converted_to_recovery(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``replace_session_events`` raises a PG deadlock
    (``OperationalError`` with ``pgcode='40P01'``), the slim task's
    ``_handle_top_level_failure`` calls ``self.retry`` with
    exponential backoff and rolls the session back to ``SEALED``.

    The deadlock is injected through the public ``FakeAnalysisPorts``
    seam (``replace_events=`` callable) and the retry is captured by
    patching the Celery ``retry`` runtime port — no internal helper
    monkey-patches.
    """

    with Session(postgres_migrated_engine) as db:
        _seed_provider(db)
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id

    def _deadlock_replace(db: Any, session_id: int, events: list[Any]) -> int:
        raise OperationalError("INSERT INTO event_record ...", {}, _DeadlockOrig())

    _container.set_analysis_ports_for_tests(
        FakeAnalysisPorts(
            chunks=1,
            sub_chunks_per_chunk=1,
            replace_events=_deadlock_replace,
        )
    )
    _set_responses(_recognition_result_json(0))

    retry_calls: list[int] = []

    def _fake_retry(*, exc: BaseException, countdown: int) -> Any:
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
) -> None:
    """A cancel that arrives between the pre / post transaction pair
    must roll the session back to ``SEALED`` (not ``PARTIAL``) and
    finalise the TaskLog as ``CANCELLED`` — the
    ``force=True`` flag in :func:`transition_session` is required for
    this system-initiated rollback even when the driving TaskLog has
    ``cancel_requested=True``.
    """

    with Session(postgres_migrated_engine) as db:
        _seed_provider(db)
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id

    side_effect_session_id = session_id

    def _on_call(call_index: int, _snapshot: dict[str, Any]) -> None:
        if call_index != 1:
            return
        with Session(postgres_migrated_engine) as flip_db:
            task_log = (
                flip_db.query(TaskLog)
                .filter(TaskLog.task_target_id == side_effect_session_id)
                .order_by(TaskLog.id.desc())
                .first()
            )
            if task_log is not None:
                task_log.cancel_requested = True
                flip_db.commit()

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    factory.install_gateway(ScriptedVisionGateway(responses=[], on_call=_on_call))
    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        _recognition_result_json(2),
    )

    result = analyze_session_task.run(session_id=session_id)
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
        # exact count is implementation-defined (>= 0; <= 2) so we
        # assert the upper bound (no events visible to consumers).
        assert len(checkpoints) <= 2


# ---------------------------------------------------------------------------
# No long-lived DB connection during the LLM HTTP call
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_no_long_checked_out_connection_during_llm_call(
    postgres_migrated_engine: Engine,
) -> None:
    """The slim Celery task opens one ``task_db_session`` for the
    orchestration lifetime but uses fresh ``task_db_session``
    contexts (``pre_db`` / ``post_db``) for every sub-chunk's
    checkpoint writes. The LLM HTTP call therefore sits between two
    connection-released windows — no checked-out connection is held
    while the gateway call runs.

    The test asserts the structural invariant: during the LLM call
    the engine pool reports **no** idle-in-transaction connection.
    The probe lives inside the scripted gateway's per-call
    side-effect hook (``on_call``), so the test does not touch any
    internal ``_analyzer_orchestration`` symbol.
    """

    with Session(postgres_migrated_engine) as db:
        _seed_provider(db)
        source = _new_source(db)
        video_session = _new_session(db, source)
        session_id = video_session.id

    snapshots: list[dict[str, int]] = []

    def _probe(call_index: int, _snapshot: dict[str, Any]) -> None:
        del call_index
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

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    _container.set_analysis_ports_for_tests(FakeAnalysisPorts(chunks=1, sub_chunks_per_chunk=1))
    factory.install_gateway(ScriptedVisionGateway(responses=[], on_call=_probe))
    _set_responses(_recognition_result_json(0))

    analyze_session_task.run(session_id=session_id)

    assert snapshots, "the LLM client was never called"
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

    url = build_sub_chunk_video_url(plans[0])
    assert url.startswith("data:video/mp4;base64,")
