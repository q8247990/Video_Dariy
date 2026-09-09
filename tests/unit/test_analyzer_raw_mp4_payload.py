"""Regression tests pinning the analyzer task to the raw_mp4-only contract.

These cover the invariants introduced when the keyframe pipeline was
removed (ADR ``0009-remove-keyframe-pipeline.md``):

* the analyzer task unconditionally builds a ``data:video/mp4`` payload
  with the documented ``num_frames`` hint — verified through the
  scripted gateway's recorded call args (the only place the
  ``extra_body`` / ``video_data_url`` surface is observable);
* the legacy keyframe branch and its payload DTO no longer exist on the
  module surface;
* ``LLMProviderBase`` rejects ``"keyframe"`` values, so the only path
  the analyzer ever sees is ``raw_mp4``.

The tests drive the public :func:`analyze_session_task` Celery entry
through the task-layer container holder; the
:class:`~src.application.bootstrap_fakes.ScriptedVisionGateway`
records every LLM call's kwargs (including the ``extra_body`` and the
video URL embedded in the user message) so the assertions read off
the recorded call log instead of patching any internal symbol.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import (
    FakeAnalysisPorts,
    ScriptedVisionGateway,
    ScriptedVisionGatewayFactory,
)
from src.models.llm_provider import LLMProvider
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis.constants import RAW_MP4_NUM_FRAMES
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks import _analyzer_orchestration, _container  # noqa: F401
from src.tasks.analyzer import analyze_session_task

SessionFactory = Callable[[], Session]


# ---------------------------------------------------------------------------
# A test (kept verbatim — pure constant pin)
# ---------------------------------------------------------------------------


def test_raw_mp4_num_frames_constant_is_120() -> None:
    """The product decision keeps the raw_mp4 frame hint at 120 frames."""

    assert RAW_MP4_NUM_FRAMES == 120


# ---------------------------------------------------------------------------
# A test (kept verbatim — module-surface keyframe pin, deferred)
# ---------------------------------------------------------------------------


def test_analyzer_module_does_not_export_keyframe_symbols() -> None:
    """The keyframe payload DTO and ``build_chunk_keyframe_payload`` helper were
    the explicit removal surface; ensure they never come back unnoticed.

    Deferred to a tier-2 architecture test in a follow-up batch — the
    pure ``hasattr`` pin is closer to a structural invariant than a
    behavioural test. Kept here for the first wave so the symbol
    removal surface stays pinned.
    """

    import src.services.session_analysis_video as sav
    import src.tasks.analyzer as analyzer_module

    assert not hasattr(sav, "build_chunk_keyframe_payload")
    assert not hasattr(sav, "ChunkKeyframePayload")
    assert not hasattr(sav, "KeyframeSet")
    assert not hasattr(analyzer_module, "build_chunk_keyframe_payload")
    assert not hasattr(analyzer_module, "keyframe_target_n")
    assert not hasattr(analyzer_module, "keyframe_fallback_count")


# ---------------------------------------------------------------------------
# A test (kept verbatim — schema ignores legacy fields)
# ---------------------------------------------------------------------------


def test_schema_silently_ignores_legacy_keyframe_fields() -> None:
    """Tod 10 retired ``video_preprocess_mode`` (and friends) from the
    schema/model/columns. Legacy clients that still POST these fields
    must be silently ignored — the schema must NOT raise, and the
    serialized payload must NOT carry the value forward (so it cannot
    reactivate the disabled capability)."""

    from src.schemas.llm_provider import LLMProviderBase

    base_kwargs = {
        "provider_name": "local",
        "api_base_url": "http://localhost:8000/v1",
        "model_name": "local-model",
        "enabled": True,
        "supports_vision": True,
    }

    obj = LLMProviderBase(
        **base_kwargs,
        video_preprocess_mode="keyframe",
        video_keyframe_target_n=128,
        video_keyframe_jpeg_quality=95,
    )
    dumped = obj.model_dump()
    assert "video_preprocess_mode" not in dumped
    assert "video_keyframe_target_n" not in dumped
    assert "video_keyframe_jpeg_quality" not in dumped


# ---------------------------------------------------------------------------
# B test (rewritten via the public seam)
# ---------------------------------------------------------------------------


def _seed_provider_and_session(db: Session) -> int:
    """Seed the minimum the analyzer task needs to claim a session."""

    provider = LLMProvider(
        provider_name="raw-mp4-test",
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
    return session.id


def _recognition_result_json() -> str:
    dto = RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text="summary",
            activity_level="medium",
            main_subjects=["爸爸"],
            has_important_event=True,
        ),
        events=[
            RecognizedEventDTO(
                offset_start_sec=1,
                offset_end_sec=5,
                event_type="member_appear",
                title="event-0",
                summary="event-0",
                detail="event-0",
                related_entities=[],
                observed_actions=[],
                interpreted_state=[],
                confidence=0.9,
                importance_level="medium",
            )
        ],
        analysis_notes=[],
    )
    return dto.model_dump_json()


def test_analyzer_emits_video_mp4_payload_with_num_frames(
    pg_db_factory: SessionFactory, monkeypatch
) -> None:
    """The analyzer payload is unconditionally ``data:video/mp4`` with the
    documented ``num_frames``; the keyframe branch is gone.

    The test reads the payload from the scripted gateway's
    :attr:`ScriptedVisionGateway.calls` log — the user message's
    ``video_url.url`` and the request's ``extra_body`` are the only
    place this surface is observable from outside the orchestrator.
    """

    session_factory = pg_db_factory
    db = session_factory()
    try:
        session_id = _seed_provider_and_session(db)
        db.commit()
    finally:
        db.close()

    factory = ScriptedVisionGatewayFactory()
    factory.install_gateway(ScriptedVisionGateway(responses=[_recognition_result_json()]))
    container = bootstrap_for_tests(llm_factory=factory)
    _container.set_container_for_tests(container)
    _container.set_analysis_ports_for_tests(
        FakeAnalysisPorts(chunks=1, sub_chunks_per_chunk=1)
    )

    @contextmanager
    def _task_session() -> Any:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.task_db_session", _task_session
    )
    try:
        analyze_session_task.run(session_id=session_id)
    finally:
        _container.reset_container_for_tests()
        _container.reset_analysis_ports_for_tests()

    gateway = factory.gateways[-1]
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    messages = call["messages"]
    assert isinstance(messages, list) and len(messages) == 2
    user_content = messages[1]["content"]
    video_part = next(item for item in user_content if item.get("type") == "video_url")
    assert video_part["video_url"]["url"].startswith("data:video/mp4;base64,")

    extra_body = call["extra_body"]
    assert extra_body is not None
    assert extra_body["media_io_kwargs"]["video"]["num_frames"] == RAW_MP4_NUM_FRAMES
    assert extra_body["media_io_kwargs"]["video"]["num_frames"] == 120
