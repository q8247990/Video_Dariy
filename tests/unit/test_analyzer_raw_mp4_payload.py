"""Regression tests pinning the analyzer task's per-mode vision payload contract.

These cover the invariants introduced when the keyframe pipeline was
removed (ADR ``0009-remove-keyframe-pipeline.md``) and extended by the
image-input / frame-sampling split (ADR
``0014-image-input-and-frame-sampling.md``):

* video mode (a provider whose ``extra_config_json`` carries NO
  ``frame_sampling`` key) — the analyzer task unconditionally builds a
  ``data:video/mp4`` payload with the documented ``num_frames`` hint;
* image mode (a provider with ``frame_sampling`` configured) — the task
  sends the interleaved ``text`` / ``image_url`` frame payload: no
  ``video_url`` part, exactly ``2*N + 1`` content parts, and no
  ``media_io_kwargs`` in the ``extra_body``;
* the legacy keyframe branch and its payload DTO no longer exist on the
  module surface;
* ``LLMProviderBase`` rejects ``"keyframe"`` values, so the only paths
  the analyzer ever sees are ``raw_mp4`` (video) and sampled frames
  (image).

The tests drive the public :func:`analyze_session_task` Celery entry
through the task-layer container holder; the
:class:`~src.application.bootstrap_fakes.ScriptedVisionGateway`
records every LLM call's kwargs (including the ``extra_body`` and the
video / image URLs embedded in the user message) so the assertions read
off the recorded call log instead of patching any internal symbol.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.application import container_holder as _container
from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import (
    FakeAnalysisPorts,
    ScriptedVisionGateway,
    ScriptedVisionGatewayFactory,
)
from src.models.llm_provider import LLMProvider
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis.contracts import RAW_MP4_NUM_FRAMES
from src.services.frame_sampling import INPUT_MODE_IMAGE, INPUT_MODE_VIDEO, resolve_input_mode
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks import _analyzer_orchestration  # noqa: F401
from src.tasks import _container as _ports_holder
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


def _seed_provider_and_session(db: Session, *, image_mode: bool = False) -> tuple[LLMProvider, int]:
    """Seed the minimum the analyzer task needs to claim a session.

    The provider's input mode is made explicit through
    ``extra_config_json``:

    * video mode (``image_mode=False``, the default) — ``extra_config_json``
      is an empty dict, i.e. it deliberately carries NO ``frame_sampling``
      key, so :func:`resolve_input_mode` resolves to ``"video"`` (the raw_mp4
      path);
    * image mode (``image_mode=True``) — ``extra_config_json`` carries the
      ``frame_sampling`` block (plus ``max_model_len`` /
      ``max_output_tokens`` for the frame budget), so
      :func:`resolve_input_mode` resolves to ``"image"``.
    """

    if image_mode:
        extra_config_json: dict[str, Any] = {
            "frame_sampling": {
                "mode": "event",
                "min_frames": 2,
                "max_frames": 12,
                "probe_fps": 2.0,
                "max_edge": 640,
                "target_context_ratio": 0.9,
            },
            "max_model_len": 16384,
            "max_output_tokens": 2048,
        }
    else:
        # Video mode, made explicit: the key exists but WITHOUT the
        # frame_sampling entry.
        extra_config_json = {}

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
        extra_config_json=extra_config_json,
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
    return provider, session.id


def _recognition_result_json() -> str:
    dto = RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text="summary",
            activity_level="medium",
            main_subjects=["爸爸"],
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
            )
        ],
        analysis_notes=[],
    )
    return dto.model_dump_json()


def test_analyzer_emits_video_mp4_payload_with_num_frames(
    pg_db_factory: SessionFactory, monkeypatch
) -> None:
    """A video-mode provider (``extra_config_json`` present but WITHOUT the
    ``frame_sampling`` key) gets the unconditional ``data:video/mp4``
    payload with the documented ``num_frames``; the keyframe branch is
    gone.

    The test reads the payload from the scripted gateway's
    :attr:`ScriptedVisionGateway.calls` log — the user message's
    ``video_url.url`` and the request's ``extra_body`` are the only
    place this surface is observable from outside the orchestrator.
    (``SubChunkRunResult.payload_mode`` is not persisted, so the
    recorded payload is the mode's observable contract.)
    """

    session_factory = pg_db_factory
    db = session_factory()
    try:
        provider, session_id = _seed_provider_and_session(db)
        # The seeded provider explicitly resolves to video mode: the
        # payload assertions below are the video-mode contract.
        assert resolve_input_mode(provider) == INPUT_MODE_VIDEO
        db.commit()
    finally:
        db.close()

    factory = ScriptedVisionGatewayFactory()
    factory.install_gateway(ScriptedVisionGateway(responses=[_recognition_result_json()]))
    container = bootstrap_for_tests(llm_factory=factory)
    _container.set_container_for_tests(container)
    _ports_holder.set_analysis_ports_for_tests(FakeAnalysisPorts(chunks=1, sub_chunks_per_chunk=1))

    @contextmanager
    def _task_session() -> Any:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr("src.tasks._analyzer_orchestration.task_db_session", _task_session)
    try:
        analyze_session_task.run(session_id=session_id)
    finally:
        _container.reset_container_for_tests()
        _ports_holder.reset_analysis_ports_for_tests()

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


def test_analyzer_emits_image_payload_for_frame_sampling_provider(
    pg_db_factory: SessionFactory, monkeypatch, tmp_path
) -> None:
    """An image-mode provider (``frame_sampling`` configured) gets the
    interleaved frame payload — the image-mode counterpart of the
    raw_mp4 pin above.

    The same fixture runs through the task with the fake frame port
    (``FakeAnalysisPorts``'s default deterministic three-frame
    ``FrameSet``); the recorded call must carry:

    * NO ``video_url`` part (the whole video is never read in image
      mode);
    * exactly ``2*N + 1 == 7`` content parts (N=3 frames) in the
      label / image / ... / prompt layout;
    * every image URL prefixed ``data:image/jpeg;base64,``;
    * NO ``media_io_kwargs`` in the ``extra_body`` (the
      ``num_frames`` hint is a video-mode-only surface).
    """

    session_factory = pg_db_factory
    # Image mode needs a real file: the orchestration computes the
    # frame-set fingerprint from the source file's content signature.
    fake_mp4 = tmp_path / "cam1.mp4"
    fake_mp4.write_bytes(b"\x00\x01\x02\x03")

    db = session_factory()
    try:
        provider, session_id = _seed_provider_and_session(db, image_mode=True)
        # The seeded provider explicitly resolves to image mode: the
        # payload assertions below are the image-mode contract.
        assert resolve_input_mode(provider) == INPUT_MODE_IMAGE
        db.commit()
    finally:
        db.close()

    factory = ScriptedVisionGatewayFactory()
    factory.install_gateway(ScriptedVisionGateway(responses=[_recognition_result_json()]))
    container = bootstrap_for_tests(llm_factory=factory)
    _container.set_container_for_tests(container)
    ports = FakeAnalysisPorts(chunks=1, sub_chunks_per_chunk=1, file_path=str(fake_mp4))
    _ports_holder.set_analysis_ports_for_tests(ports)

    @contextmanager
    def _task_session() -> Any:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr("src.tasks._analyzer_orchestration.task_db_session", _task_session)
    try:
        result = analyze_session_task.run(session_id=session_id)
    finally:
        _container.reset_container_for_tests()
        _ports_holder.reset_analysis_ports_for_tests()

    # The run completed through the image path (one file -> one event).
    assert result["events_created"] == 1
    # The frame port (fake) was called exactly once, for the one file.
    assert len(ports.frame_set_calls) == 1
    assert ports.frame_set_calls[0][1] == str(fake_mp4)

    gateway = factory.gateways[-1]
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    messages = call["messages"]
    assert isinstance(messages, list) and len(messages) == 2
    user_content = messages[1]["content"]

    # No video payload in image mode.
    assert all(item.get("type") != "video_url" for item in user_content)
    # 2*N+1 interleaved parts for the fake port's N=3 frames:
    # label, image, ..., label, image, prompt.
    assert len(user_content) == 2 * 3 + 1
    assert [item["type"] for item in user_content] == ["text", "image_url"] * 3 + ["text"]
    for item in user_content:
        if item["type"] == "image_url":
            assert item["image_url"]["url"].startswith("data:image/jpeg;base64,")

    # The video sampling hint must not leak into the image payload.
    assert "media_io_kwargs" not in (call["extra_body"] or {})
    assert call["extra_body"] == {}
