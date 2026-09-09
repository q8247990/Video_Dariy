"""Session analysis Celery task — public-entry tests (Todo "测试边界收敛" first wave).

The Celery task ``analyze_session_task`` is exercised end-to
end against real PostgreSQL (the
``pg_db_factory`` / ``pg_db`` fixtures) and a scripted
:class:`~src.application.ports.llm_gateway.LLMGatewayFactoryPort`
injected through the task-layer container holder
(:func:`src.tasks._container.set_container_for_tests`). The analyzer
pipeline's media-layout helpers are wired through
:class:`~src.application.bootstrap_fakes.FakeAnalysisPorts` and the
:class:`~src.application.bootstrap_fakes.ScriptedVisionGateway`
scripted responses, so no test reaches into ``src.tasks._analyzer_orchestration``
module namespace.

The only allowed monkey-patches against ``src`` internals are:

* :func:`src.tasks._analyzer_orchestration.task_db_session` (DB-env
  seam);
* ``analyze_session_task.retry`` (Celery runtime port for deadlock /
  retry injection);
* ``outbox_contracts._reset_emitted_event_ids_for_testing`` (authorized
  test hook — listed for completeness even though no test in this file
  uses it).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pytest
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
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis.checkpoint_writer import _checkpoint_for_work
from src.services.pipeline_constants import TaskStatus
from src.services.provider_key_crypto import ProviderKeyDecryptionError
from src.services.session_analysis_video import SubChunk
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks import _analyzer_orchestration, _container  # noqa: F401
from src.tasks.analyzer import analyze_session_task

SessionFactory = Callable[[], Session]


# ---------------------------------------------------------------------------
# Base action — module-scoped fixture that prepares the task-layer holder
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _base_action(pg_db_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire the task-layer container + ports holder for every test.

    The standard scripted gateway exposes every call (full kwargs
    including ``extra_body`` and the recorded raw response text) in
    :attr:`ScriptedVisionGateway.calls` and serves any responses a test
    queues via :meth:`ScriptedVisionGateway.queue_responses`. The
    standard :class:`FakeAnalysisPorts` resolves to 1 chunk × 3
    sub-chunks sharing the file path ``"/tmp/mock.mp4"`` (the
    fingerprint is therefore stable across runs). Tests that need a
    different shape inject a customised :class:`FakeAnalysisPorts`
    *before* queuing responses.

    ``task_db_session`` is monkey-patched to route the analyzer task's
    DB connections through the ``pg_db_factory`` so the seeded rows
    (in ``postgres_real_schema``) are visible from the orchestrator.
    This is the only allowed monkey-patch against the analyzer
    internals (listed in the module header).
    """

    factory = ScriptedVisionGatewayFactory()
    gateway = ScriptedVisionGateway(responses=[])
    factory.install_gateway(gateway)
    container = bootstrap_for_tests(llm_factory=factory)
    _container.set_container_for_tests(container)
    _container.set_analysis_ports_for_tests(FakeAnalysisPorts())

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = pg_db_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.task_db_session", _task_session
    )

    try:
        yield
    finally:
        _container.reset_container_for_tests()
        _container.reset_analysis_ports_for_tests()


def _seed_provider(db: Session) -> int:
    """Seed the enabled default-vision :class:`LLMProvider`.

    The scripted gateway bypasses decryption so an empty ``api_key`` is
    fine; ``is_default_vision`` + ``supports_vision`` + ``enabled`` are
    the only fields :func:`find_required_enabled_provider` checks.
    """
    provider = LLMProvider(
        provider_name="analyzer-task-test",
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
    db.flush()
    return provider.id


def _seed_source_and_session(db: Session) -> tuple[int, int]:
    source = VideoSource(
        source_name="客厅",
        camera_name="cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.flush()

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 15, 10, 0, 0),
        session_end_time=datetime(2026, 3, 15, 10, 5, 0),
        total_duration_seconds=300,
        analysis_status="sealed",
    )
    db.add(session)
    db.flush()
    return source.id, session.id


def _seed_old_event(
    db: Session, source_id: int, session_id: int, description: str = "old event"
) -> int:
    event = EventRecord(
        source_id=source_id,
        session_id=session_id,
        event_start_time=datetime(2026, 3, 15, 10, 0, 10),
        event_end_time=datetime(2026, 3, 15, 10, 0, 20),
        description=description,
        event_type="member_appear",
        action_type="member_appear",
        title=description,
        summary=description,
        confidence_score=0.8,
        offset_start_sec=10,
        offset_end_sec=20,
    )
    db.add(event)
    db.flush()
    return event.id


def _recognition_result(index: int) -> RecognitionResultDTO:
    return RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text=f"summary-{index}",
            activity_level="medium",
            main_subjects=["爸爸"],
            has_important_event=True,
        ),
        events=[
            RecognizedEventDTO(
                offset_start_sec=1,
                offset_end_sec=5,
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


def _recognition_result_json(index: int) -> str:
    """Serialised :class:`RecognitionResultDTO` consumed by the real parser.

    The analyzer pipeline calls
    :func:`src.services.video_analysis.output_parser.parse_video_recognition_output`
    on the gateway's raw response text, so every test scripts the
    response with this JSON and lets the real parser produce the
    :class:`RecognitionResultDTO` it stores.
    """
    return _recognition_result(index).model_dump_json()


def _latest_gateway() -> ScriptedVisionGateway:
    """Return the gateway the latest run consumed (base action rebuilds per test)."""

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    return factory.gateways[-1]


def _set_responses(*responses: str) -> None:
    """Queue scripted responses on the gateway the next run will consume.

    Preserves the existing gateway's ``on_call`` side-effect hook when
    the same gateway is re-used; falls back to installing a fresh
    gateway when no installed gateway exists.
    """

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    if factory._gateway is not None:
        factory._gateway.queue_responses(list(responses))
    else:
        # No installed gateway: install a fresh one. The ``on_call``
        # hook is intentionally lost in this branch because there was
        # no prior gateway carrying one.
        factory.install_gateway(ScriptedVisionGateway(responses=list(responses)))


# ---------------------------------------------------------------------------
# Happy / failure / cancel paths
# ---------------------------------------------------------------------------


def test_analyze_session_partial_failure_preserves_completed_sub_chunks(
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        "raise",
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        analyze_session_task.run(session_id=session_id)

    verify_db = session_factory()
    try:
        checkpoints = (
            verify_db.query(SessionAnalysisCheckpoint)
            .order_by(SessionAnalysisCheckpoint.sub_chunk_index)
            .all()
        )
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert [checkpoint.state for checkpoint in checkpoints] == ["success", "success", "error"]
        assert checkpoints[0].event_payload is not None
        assert checkpoints[1].total_tokens == 15
        assert checkpoints[2].last_error == "provider failed"
        assert checkpoints[2].error_type == "RuntimeError"
        assert session.analysis_status == "partial"
        assert (
            verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 0
        )
    finally:
        verify_db.close()


def test_analyze_session_retry_resumes_from_first_non_success_without_recharging(
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        "raise",
    )
    with pytest.raises(RuntimeError):
        analyze_session_task.run(session_id=session_id)
    first_call_count = len(_latest_gateway().calls)

    _set_responses(_recognition_result_json(2))
    result = analyze_session_task.run(session_id=session_id)

    verify_db = session_factory()
    try:
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        checkpoints = verify_db.query(SessionAnalysisCheckpoint).all()
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        # First run reached all three sub-chunks (two successes, one raising).
        assert first_call_count == 3
        # Retry only called the LLM once more (the failed sub-chunk).
        retry_calls = _latest_gateway().calls
        assert len(retry_calls) == 1
        assert result["events_created"] == 3
        assert session.analysis_status == "success"
        assert len(checkpoints) == 3
        assert sum(checkpoint.total_tokens for checkpoint in checkpoints) == 45
        assert len(events) == 3
        assert {event.summary for event in events} == {"event-0", "event-1", "event-2"}
    finally:
        verify_db.close()


def test_analyze_session_duplicate_retry_does_not_duplicate_events_or_usage(
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        _recognition_result_json(2),
    )
    analyze_session_task.run(session_id=session_id)
    calls_after_first = sum(len(g.calls) for g in _container.get_container().llm_factory.gateways)
    retry_result = analyze_session_task.run(session_id=session_id)
    calls_after_second = sum(len(g.calls) for g in _container.get_container().llm_factory.gateways)

    verify_db = session_factory()
    try:
        assert calls_after_first == 3
        # Second run made zero new LLM calls (claim is skipped because
        # the session is no longer SEALED / PARTIAL).
        assert calls_after_second == calls_after_first
        assert retry_result["skipped"] is True
        assert (
            verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 3
        )
        assert verify_db.query(SessionAnalysisCheckpoint).count() == 3
    finally:
        verify_db.close()


def test_analyze_session_changed_input_invalidates_stale_checkpoints(
    pg_db_factory: SessionFactory,
) -> None:
    """Whitebox pin for the input-fingerprint-invalidation branch.

    Drives :func:`src.services.analysis.checkpoint_writer._checkpoint_for_work`
    directly to keep the assertion local to the legacy kwargs-based
    signature. The production pipeline uses
    :func:`src.services.analysis.checkpoint_writer.get_or_create_checkpoint`
    (the slim Celery task drives it through the public AnalysisPorts
    seam), but the input-fingerprint reset is shared logic and the
    kwargs-based wrapper is the only public access to it; we keep this
    as a whitebox test until the legacy wrapper is retired.
    """

    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        _, session_id = _seed_source_and_session(db)
        checkpoint = SessionAnalysisCheckpoint(
            session_id=session_id,
            analysis_run_id="run",
            chunk_index=0,
            sub_chunk_index=0,
            start_offset_seconds=0,
            input_fingerprint="old",
            state="success",
            event_payload={"events": []},
            total_tokens=15,
        )
        db.add(checkpoint)
        db.commit()
        sub_chunk = SubChunk(0, 0, 0, 60, ["/tmp/mock.mp4"])
        updated = _checkpoint_for_work(
            db,
            session_id=session_id,
            analysis_run_id="run",
            chunk_index=0,
            sub_chunk=sub_chunk,
            input_fingerprint="new",
        )
        assert updated.state == "pending"
        assert updated.event_payload is None
        assert updated.total_tokens == 0
    finally:
        db.close()


def test_analyze_session_cancelled_mid_run_partial_not_served(
    pg_db_factory: SessionFactory,
) -> None:
    """A cancel that arrives during the LLM call is honoured by
    :func:`ensure_task_not_cancelled`. The scripted gateway's per-call
    side effect flips ``TaskLog.cancel_requested=True`` *after* the
    second LLM call so the next fence raises
    :class:`TaskCancellationRequested`.
    """

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    db = pg_db_factory()
    try:
        _seed_provider(db)
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    def _on_call(call_index: int, _snapshot: dict[str, Any]) -> None:
        if call_index != 1:
            return
        # Use a one-off session so we don't hold a connection while the
        # gateway call is in flight.
        with pg_db_factory() as flip_db:
            task_log = (
                flip_db.query(TaskLog)
                .filter(TaskLog.task_target_id == session_id)
                .order_by(TaskLog.id.desc())
                .first()
            )
            assert task_log is not None
            task_log.cancel_requested = True
            flip_db.commit()

    factory.install_gateway(ScriptedVisionGateway(responses=[], on_call=_on_call))
    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        _recognition_result_json(2),
    )

    result = analyze_session_task.run(session_id=session_id)
    assert result["cancelled"] is True

    verify_db = pg_db_factory()
    try:
        assert (
            verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 1
        )
        checkpoints = verify_db.query(SessionAnalysisCheckpoint).all()
        assert len(checkpoints) == 2
    finally:
        verify_db.close()


def test_analyze_session_replaces_old_events(pg_db_factory: SessionFactory) -> None:
    _container.set_analysis_ports_for_tests(
        FakeAnalysisPorts(chunks=1, sub_chunks_per_chunk=1)
    )
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="old")
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(7))
    result = analyze_session_task.run(session_id=session_id)
    assert result["events_created"] == 1

    verify_db = session_factory()
    try:
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        assert len(events) == 1
        assert events[0].summary == "event-7"
    finally:
        verify_db.close()


def test_analyze_session_empty_result_clears_old_events(
    pg_db_factory: SessionFactory,
) -> None:
    """The parser returns :class:`RecognitionResultDTO` with ``events=[]`` so the
    real persistence path (atomic delete-then-insert) lands zero rows.
    """

    empty_dto = RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text="no event",
            activity_level="low",
            main_subjects=[],
            has_important_event=False,
        ),
        events=[],
        analysis_notes=[],
    )

    _container.set_analysis_ports_for_tests(
        FakeAnalysisPorts(chunks=1, sub_chunks_per_chunk=1)
    )
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id)
        db.commit()
    finally:
        db.close()

    _set_responses(empty_dto.model_dump_json())
    result = analyze_session_task.run(session_id=session_id)
    assert result["events_created"] == 0

    verify_db = session_factory()
    try:
        count = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count()
        assert count == 0
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).first()
        assert session is not None
        assert session.analysis_status == "success"
    finally:
        verify_db.close()


def test_analyze_session_failure_keeps_old_events(pg_db_factory: SessionFactory) -> None:
    """The parser raises :class:`ValueError` on malformed JSON, so the
    real :func:`parse_video_recognition_output` is the failure injection
    point — no internal patch.
    """

    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    _set_responses("not-valid-json")

    with pytest.raises(ValueError):
        analyze_session_task.run(session_id=session_id)

    verify_db = session_factory()
    try:
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        assert len(events) == 1
        assert events[0].summary == "keep me"
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).first()
        assert session is not None
        assert session.analysis_status == "partial"
    finally:
        verify_db.close()


def test_analyze_session_failure_logs_prompt_and_raw_response(
    pg_db_factory: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """The real :func:`parse_video_recognition_output` raises on
    malformed JSON; the failure log must capture the real prompt
    (built from the seeded ``VideoSource.prompt_text``) and the
    gateway-reported raw response text.
    """

    caplog.set_level(logging.ERROR, logger="src.tasks._analyzer_orchestration")
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        source = VideoSource(
            source_name="客厅",
            camera_name="cam1",
            location_name="客厅",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            prompt_text="final prompt marker-MARK42",
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 15, 10, 0, 0),
            session_end_time=datetime(2026, 3, 15, 10, 5, 0),
            total_duration_seconds=300,
            analysis_status="sealed",
        )
        db.add(session)
        db.flush()
        session_id = session.id
        _seed_old_event(db, source.id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    factory.install_gateway(
        ScriptedVisionGateway(
            responses=["not-valid-json"],
            raw_response_text='{"choices":[{"message":{"content":null,"reasoning":"debug-marker"}}]}',
        )
    )

    with pytest.raises(ValueError):
        analyze_session_task.run(session_id=session_id)

    assert "final prompt marker-MARK42" in caplog.text
    assert '"reasoning":"debug-marker"' in caplog.text


def test_analyze_session_deadlock_retries_with_exponential_backoff(
    pg_db_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject the deadlock via the FakeAnalysisPorts seam (no internal patch).

    The fake ``replace_events`` raises :class:`OperationalError` with
    ``pgcode='40P01'``; the analyzer detects it, rolls the session
    back to SEALED and calls ``self.retry``.
    """

    class _DeadlockOrig(Exception):
        def __init__(self) -> None:
            self.pgcode = "40P01"

        def __str__(self) -> str:
            return "deadlock detected"

    def _deadlock_replace(db: Any, session_id: int, events: list[Any]) -> int:
        raise OperationalError("INSERT INTO event_record ...", {}, _DeadlockOrig())

    _container.set_analysis_ports_for_tests(
        FakeAnalysisPorts(
            chunks=1,
            sub_chunks_per_chunk=1,
            replace_events=_deadlock_replace,
        )
    )
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(0))

    retry_calls: list[int] = []

    def _fake_retry(*, exc: BaseException, countdown: int) -> Any:
        retry_calls.append(countdown)
        raise RuntimeError("retry-triggered")

    monkeypatch.setattr(analyze_session_task, "retry", _fake_retry, raising=False)
    analyze_session_task.push_request(id="retry-task-id", retries=0)
    try:
        with pytest.raises(RuntimeError, match="retry-triggered"):
            analyze_session_task.run(session_id=session_id)
    finally:
        analyze_session_task.pop_request()

    verify_db = session_factory()
    try:
        task_log = verify_db.query(TaskLog).filter(TaskLog.task_target_id == session_id).one()
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()

        assert retry_calls == [1]
        assert task_log.retry_count == 1
        assert "Deadlock detected, retry 1/3 in 1s" in (task_log.message or "")
        assert session.analysis_status == "sealed"
        assert len(events) == 1
        assert events[0].summary == "keep me"
    finally:
        verify_db.close()


def test_analyze_session_skips_when_already_analyzing(pg_db_factory: SessionFactory) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        session = db.query(VideoSession).filter(VideoSession.id == session_id).one()
        session.analysis_status = "analyzing"
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(0))
    result = analyze_session_task.run(session_id=session_id)
    assert result["skipped"] is True
    assert result["reason"] == "already_analyzing"

    verify_db = session_factory()
    try:
        task_log = verify_db.query(TaskLog).filter(TaskLog.task_target_id == session_id).one()
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        assert task_log.status == TaskStatus.SKIPPED
        assert task_log.message == f"Skipped session {session_id}, already analyzing"
        assert session.analysis_status == "analyzing"
        assert len(events) == 1
        assert events[0].summary == "keep me"
    finally:
        verify_db.close()


def test_analyze_session_honors_cancel_requested(pg_db_factory: SessionFactory) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        task_log = TaskLog(
            task_type="session_analysis",
            task_target_id=session_id,
            queue_task_id="cancel-task-id",
            status="running",
            cancel_requested=True,
            detail_json={"priority": "hot", "dedupe_key": f"session_analysis|{session_id}"},
        )
        db.add(task_log)
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(0))

    analyze_session_task.push_request(id="cancel-task-id", retries=0)
    try:
        result = analyze_session_task.run(session_id=session_id)
    finally:
        analyze_session_task.pop_request()

    assert result["cancelled"] is True

    verify_db = session_factory()
    try:
        task_log = verify_db.query(TaskLog).filter(TaskLog.task_target_id == session_id).one()
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        assert task_log.status == TaskStatus.CANCELLED
        assert task_log.cancel_requested is True
        assert session.analysis_status == "sealed"
        assert len(events) == 1
        assert events[0].summary == "keep me"
    finally:
        verify_db.close()


def test_provider_build_failure_finalizes_task_log_failed(
    pg_db_factory: SessionFactory,
) -> None:
    """A failure during provider-client build (e.g. API-key decryption) must
    finalize the TaskLog to FAILED, not strand it a stranded "running" row
    with no ``finished_at``/``message``.
    """

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)

    class _FailingFactory:
        def build(self, **_kwargs: Any) -> ScriptedVisionGateway:
            raise ProviderKeyDecryptionError("cannot decrypt")

    container = bootstrap_for_tests(llm_factory=_FailingFactory())
    _container.set_container_for_tests(container)

    session_factory = pg_db_factory
    db = session_factory()
    try:
        _seed_provider(db)
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    with pytest.raises(ProviderKeyDecryptionError):
        analyze_session_task.run(session_id=session_id, priority="hot")

    verify_db = session_factory()
    try:
        task_log = (
            verify_db.query(TaskLog)
            .filter(
                TaskLog.task_target_id == session_id,
                TaskLog.task_type == "session_analysis",
            )
            .one()
        )
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert task_log.status == TaskStatus.FAILED
        assert task_log.finished_at is not None
        assert task_log.message is not None
        assert session.analysis_status == "failed"
    finally:
        verify_db.close()
