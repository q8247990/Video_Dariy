"""PostgreSQL integration tests for the analyzer checkpoint table (Todo "测试边界收敛" first wave).

These tests drive the public :func:`analyze_session_task` Celery
entry against a real PostgreSQL instance and a scripted
:class:`~src.application.ports.llm_gateway.LLMGatewayFactoryPort`
injected through the task-layer container holder. The analyzer
pipeline's media-layout helpers are wired through
:class:`~src.application.bootstrap_fakes.FakeAnalysisPorts`.

The only allowed monkey-patch against ``src`` internals is:

* :func:`src.tasks._analyzer_orchestration.task_db_session` (DB-env
  seam) — replaced once per test with a context manager that opens a
  Session against the migrated engine.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import pytest
from sqlalchemy.exc import IntegrityError
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
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks import _analyzer_orchestration, _container  # noqa: F401
from src.tasks.analyzer import analyze_session_task


@pytest.fixture(autouse=True)
def _base_action(postgres_migrated_engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Wire the task-layer container + ports holder for every test.

    Installs a scripted vision factory with an empty-responses gateway
    and a 1-chunk × 3-sub-chunk :class:`FakeAnalysisPorts`. Tests
    queue responses or override the ports before invoking the task.
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

    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.task_db_session", _task_session
    )
    try:
        yield
    finally:
        _container.reset_container_for_tests()
        _container.reset_analysis_ports_for_tests()


def _seed_provider(db: Session) -> LLMProvider:
    provider = LLMProvider(
        provider_name="analyzer-checkpoint-pg",
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
    return provider


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
    return _recognition_result(index).model_dump_json()


def _set_responses(*responses: str) -> None:
    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    if factory._gateway is not None:
        factory._gateway.queue_responses(list(responses))
    else:
        factory.install_gateway(ScriptedVisionGateway(responses=list(responses)))


# ---------------------------------------------------------------------------
# A test (kept)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_analysis_checkpoint_unique_work_key_on_postgres(postgres_migrated_engine) -> None:
    """The ``(session_id, analysis_run_id, chunk_index, sub_chunk_index)``
    partial unique key on :class:`SessionAnalysisCheckpoint` rejects
    duplicate work-key rows.
    """

    db = Session(bind=postgres_migrated_engine)
    try:
        source = VideoSource(
            source_name="PG camera",
            camera_name="PG camera",
            location_name="home",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime.now(timezone.utc),
            session_end_time=datetime.now(timezone.utc),
            analysis_status="sealed",
        )
        db.add(session)
        db.flush()
        first = SessionAnalysisCheckpoint(
            session_id=session.id,
            analysis_run_id="run",
            chunk_index=0,
            sub_chunk_index=0,
            start_offset_seconds=0,
            input_fingerprint="fingerprint",
            state="success",
        )
        db.add(first)
        db.flush()
        duplicate = SessionAnalysisCheckpoint(
            session_id=session.id,
            analysis_run_id="run",
            chunk_index=0,
            sub_chunk_index=0,
            start_offset_seconds=0,
            input_fingerprint="fingerprint",
            state="success",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# B test (rewritten via the public seam)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_analyze_session_failure_then_retry_resumes_only_remaining_on_postgres(
    postgres_migrated_engine,
) -> None:
    """Three sub-chunks: two succeed, one fails. The retry resumes only the
    remaining sub-chunk — the gateway's call log proves no LLM HTTP
    request was re-issued for the already-success sub-chunks.
    """

    db = Session(bind=postgres_migrated_engine)
    try:
        _seed_provider(db)
        source = VideoSource(
            source_name="PG retry camera",
            camera_name="PG retry camera",
            location_name="home",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime.now(timezone.utc),
            session_end_time=datetime.now(timezone.utc),
            total_duration_seconds=180,
            analysis_status="sealed",
        )
        db.add(session)
        db.commit()
        session_id = session.id
    finally:
        db.close()

    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        "raise",
    )
    with pytest.raises(RuntimeError, match="provider failed"):
        analyze_session_task.run(session_id=session_id)

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    first_call_count = len(factory.gateways[-1].calls)

    verify = Session(bind=postgres_migrated_engine)
    try:
        checkpoints = (
            verify.query(SessionAnalysisCheckpoint)
            .order_by(SessionAnalysisCheckpoint.sub_chunk_index)
            .all()
        )
        failed_session = verify.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert first_call_count == 3
        assert [checkpoint.state for checkpoint in checkpoints] == ["success", "success", "error"]
        assert sum(checkpoint.total_tokens for checkpoint in checkpoints) == 30
        assert failed_session.analysis_status == "partial"
        assert verify.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 0
        assert verify.query(LLMUsageLog).filter(LLMUsageLog.session_id == session_id).count() == 2
    finally:
        verify.close()

    _set_responses(_recognition_result_json(2))
    result = analyze_session_task.run(session_id=session_id)

    retry_call_count = len(factory.gateways[-1].calls)
    verify = Session(bind=postgres_migrated_engine)
    try:
        completed_session = verify.query(VideoSession).filter(VideoSession.id == session_id).one()
        events = verify.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        usage = verify.query(LLMUsageLog).filter(LLMUsageLog.session_id == session_id).all()
        assert retry_call_count == 1
        assert result["events_created"] == 3
        assert completed_session.analysis_status == "success"
        assert len(events) == 3
        assert {event.summary for event in events} == {"event-0", "event-1", "event-2"}
        assert len(usage) == 3
        assert sum(item.total_tokens for item in usage) == 45
    finally:
        verify.close()
