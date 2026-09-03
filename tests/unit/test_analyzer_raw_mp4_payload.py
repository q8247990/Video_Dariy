"""Regression tests pinning the analyzer task to the raw_mp4-only contract.

These cover the invariants introduced when the keyframe pipeline was
removed (ADR ``0009-remove-keyframe-pipeline.md``):

* the analyzer task unconditionally builds a ``data:video/mp4`` payload
  with the documented ``num_frames`` hint;
* the legacy keyframe branch and its payload DTO no longer exist on the
  module surface;
* ``LLMProviderBase`` rejects ``"keyframe"`` values, so the only path
  the analyzer ever sees is ``raw_mp4``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.session_analysis_video import SessionVideoChunk, SubChunk
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks.analyzer import RAW_MP4_NUM_FRAMES, analyze_session_task

SessionFactory = Callable[[], Session]


def _seed_source_and_session(db) -> int:
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


def test_raw_mp4_num_frames_constant_is_120() -> None:
    """The product decision keeps the raw_mp4 frame hint at 120 frames."""

    assert RAW_MP4_NUM_FRAMES == 120


def test_analyzer_emits_video_mp4_payload_with_num_frames(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    """The analyzer payload is unconditionally ``data:video/mp4`` with the
    documented ``num_frames``; the keyframe branch is gone."""

    session_factory = pg_db_factory
    db = session_factory()
    try:
        session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    captured: dict[str, object] = {}

    class _FakeClient:
        def chat_completion(self, *, messages, extra_body, **_kwargs):
            del _kwargs
            captured["messages"] = messages
            captured["extra_body"] = extra_body
            return "{}"

        def get_last_usage(self):
            return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        def get_last_raw_response_text(self):
            return "raw"

    from contextlib import contextmanager

    @contextmanager
    def _fake_task_db_session():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr("src.tasks.analyzer.task_db_session", _fake_task_db_session)
    monkeypatch.setattr(
        "src.tasks.analyzer.build_session_video_chunks",
        lambda db, session_id, chunk_seconds: [
            SessionVideoChunk(
                chunk_index=0,
                start_offset_seconds=0,
                duration_seconds=60,
                file_paths=["/tmp/mock.mp4"],
            )
        ],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_sub_chunks",
        lambda chunk, db, sub_chunk_seconds: [
            SubChunk(
                chunk_index=0,
                sub_chunk_index=0,
                start_offset_seconds=0,
                duration_seconds=60,
                file_paths=["/tmp/mock.mp4"],
            )
        ],
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.build_chunk_video_data_url",
        lambda chunk: "data:video/mp4;base64,AAA",
    )
    monkeypatch.setattr(
        "src.tasks.analyzer._build_provider_client",
        lambda db: (_FakeClient(), SimpleNamespace(id=1, provider_name="mock-provider")),
    )
    monkeypatch.setattr("src.tasks.analyzer.build_home_context", lambda db: {})
    monkeypatch.setattr("src.tasks.analyzer.enforce_token_quota", lambda db, provider: None)
    monkeypatch.setattr(
        "src.tasks.analyzer.record_token_usage",
        lambda db, provider_id, provider_name_snapshot, scene, usage, **kwargs: None,
    )
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
        lambda response: _recognition_result(0),
    )

    analyze_session_task.run(session_id=session_id)

    messages = captured["messages"]
    assert isinstance(messages, list) and len(messages) == 2
    user_content = messages[1]["content"]
    video_part = next(item for item in user_content if item.get("type") == "video_url")
    assert video_part["video_url"]["url"].startswith("data:video/mp4;base64,")

    extra_body = captured["extra_body"]
    assert extra_body is not None
    assert extra_body["media_io_kwargs"]["video"]["num_frames"] == RAW_MP4_NUM_FRAMES
    assert extra_body["media_io_kwargs"]["video"]["num_frames"] == 120


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


def test_analyzer_module_does_not_export_keyframe_symbols() -> None:
    """The keyframe payload DTO and ``build_chunk_keyframe_payload`` helper were
    the explicit removal surface; ensure they never come back unnoticed."""

    import src.services.session_analysis_video as sav
    import src.tasks.analyzer as analyzer_module

    assert not hasattr(sav, "build_chunk_keyframe_payload")
    assert not hasattr(sav, "ChunkKeyframePayload")
    assert not hasattr(sav, "KeyframeSet")
    assert not hasattr(analyzer_module, "build_chunk_keyframe_payload")
    assert not hasattr(analyzer_module, "keyframe_target_n")
    assert not hasattr(analyzer_module, "keyframe_fallback_count")
