"""Wave D (todo 24): the image-mode output contract is invariant.

ADR 0014 defines two vision payload modes (raw_mp4 video vs sampled-frame
image) that must be interchangeable *downstream*: both feed the same
parser (:func:`parse_video_recognition_output`), the same
:class:`RecognitionResultDTO` field set, the same absolute-time offset
math and the same ``EventRecord`` / daily summary / QA / MCP consumers.
These tests pin that contract:

1. field-level invariance of the DTO shape (pinned literal field set +
   ``model_dump()`` equality for the same sample in both modes, driven
   end-to-end through the :func:`execute_sub_chunk` seam);
2. offset mapping (``base_offset_seconds + offset`` == absolute session
   time, identical for both modes);
3. rejection of events whose offsets fall outside the sub-chunk window
   (the boundary guard added for this wave — the parser alone cannot
   know the duration);
4. the finalize/replace path persists events with the absolute
   ``base_offset + offset`` times;
5. a diff guard: a pinned sha256 of ``schemas.py`` so any future schema
   edit fails here and forces a deliberate ADR 0014 re-review.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.application.bootstrap_fakes import ScriptedVisionGateway
from src.models.event_record import EventRecord
from src.models.llm_provider import LLMProvider
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis.execute import execute_sub_chunk, frames_mode_frame_fingerprint
from src.services.analysis.finalize import completed_results_for_run, replace_session_events
from src.services.analysis.plan import SubChunkPlan
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import FrameSample
from src.services.video_analysis.mapper import (
    build_event_record_from_recognized_event,
    validate_recognition_offsets_within_sub_chunk,
)
from src.services.video_analysis.output_parser import (
    RecognitionOutputValidationError,
    parse_video_recognition_output,
)
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    RelatedEntityDTO,
    SessionSummaryDTO,
)

# ---------------------------------------------------------------------------
# Pinned contract constants (todo 24)
# ---------------------------------------------------------------------------

# The exact field set the downstream contract (EventRecord / daily summary /
# QA / MCP) consumes. Pasted as a literal on purpose: any addition, removal
# or rename in src/services/video_analysis/schemas.py must be a deliberate
# ADR 0014 decision, and the diff guard below fails until the pin is updated.
EXPECTED_RECOGNITION_RESULT_FIELDS = frozenset({"session_summary", "events", "analysis_notes"})
EXPECTED_SESSION_SUMMARY_FIELDS = frozenset({"summary_text", "activity_level", "main_subjects"})
EXPECTED_RECOGNIZED_EVENT_FIELDS = frozenset(
    {
        "offset_start_sec",
        "offset_end_sec",
        "event_type",
        "title",
        "summary",
        "detail",
        "related_entities",
        "observed_actions",
        "interpreted_state",
        "focus_matches",
        "confidence",
    }
)
EXPECTED_RELATED_ENTITY_FIELDS = frozenset(
    {
        "entity_type",
        "display_name",
        "matched_profile_name",
        "candidate_profile_names",
        "recognition_status",
        "confidence",
    }
)

# A fixed, fully-populated LLM JSON sample: every DTO field is exercised so
# a field added/removed on the schema changes both the parse result and the
# diff guard below.
FIXED_RECOGNITION_JSON = """
{
  "session_summary": {
    "summary_text": "客厅有人经过",
    "activity_level": "medium",
    "main_subjects": ["爸爸", "栗子"]
  },
  "events": [
    {
      "offset_start_sec": 0,
      "offset_end_sec": 5,
      "event_type": "member_appear",
      "title": "爸爸进入客厅",
      "summary": "爸爸从门口进入客厅",
      "detail": "爸爸从门口进入，经过茶几后在沙发附近停留。",
      "related_entities": [
        {
          "entity_type": "member",
          "display_name": "爸爸",
          "matched_profile_name": "爸爸",
          "recognition_status": "confirmed",
          "confidence": 0.95
        }
      ],
      "observed_actions": ["walk", "enter"],
      "interpreted_state": ["normal"],
      "focus_matches": ["member_home"],
      "confidence": 0.9
    },
    {
      "offset_start_sec": 55,
      "offset_end_sec": 60,
      "event_type": "pet_appear",
      "title": "猫咪出现",
      "summary": "栗子出现在沙发旁",
      "detail": "栗子从沙发下方出现并走动。",
      "related_entities": [],
      "observed_actions": ["move"],
      "interpreted_state": ["normal"],
      "focus_matches": [],
      "confidence": 0.8
    }
  ],
  "analysis_notes": [
    {"type": "low_visibility", "note": "画面略暗"}
  ]
}
"""

# Diff guard: sha256 of the *normalized* (line-rstripped, trailing blank
# lines dropped) source of src/services/video_analysis/schemas.py. The pin
# is intentional — it must be updated deliberately, only after re-verifying
# that the image-mode output contract (ADR 0014) remains invariant.
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "services" / "video_analysis" / "schemas.py"
)
PINNED_SCHEMAS_SHA256 = "af4eefd38f6a59cd993f87d6a07292cb0ca9764019fccffd1c813350b96ee3f2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider(**extra: Any) -> LLMProvider:
    """A lightweight in-memory vision provider (no DB)."""
    return LLMProvider(
        provider_name="contract-invariance-test",
        provider_type="vision_provider",
        api_base_url="http://example.invalid/v1",
        api_key="",
        model_name="vision-test",
        timeout_seconds=30,
        enabled=True,
        supports_vision=True,
        is_default_vision=True,
        **extra,
    )


def _sub_chunk() -> SubChunkPlan:
    return SubChunkPlan(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=("/tmp/fake.mp4",),
    )


def _frames() -> list[FrameSample]:
    return [
        FrameSample(t_seconds=0.0, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-A", reason="uniform"),
        FrameSample(t_seconds=30.0, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-B", reason="uniform"),
    ]


def _run_execute(*, frames: Any) -> Any:
    """Drive the execute seam with the same scripted response in one mode."""
    client = ScriptedVisionGateway(responses=[FIXED_RECOGNITION_JSON])
    frame_fingerprint = (
        frames_mode_frame_fingerprint(frames, FramePolicy(), content_signature="unit-test")
        if frames
        else None
    )
    return execute_sub_chunk(
        client=client,
        provider=_provider(),
        sub_chunk=_sub_chunk(),
        system_prompt="system",
        user_prompt="请逐帧描述画面",
        video_data_url="data:video/mp4;base64,AAAA",
        frames=frames,
        frame_fingerprint=frame_fingerprint,
    )


def _session() -> VideoSession:
    return VideoSession(
        id=7,
        source_id=3,
        session_start_time=datetime(2026, 3, 14, 10, 0, 0),
        session_end_time=datetime(2026, 3, 14, 10, 30, 0),
        total_duration_seconds=1800,
        analysis_status="sealed",
    )


# ---------------------------------------------------------------------------
# 1. Field-level invariance
# ---------------------------------------------------------------------------


def test_recognition_dto_field_set_is_pinned() -> None:
    """The DTO field set is exactly the literal set the contract pins.

    Adding, removing or renaming a field in schemas.py fails here (and in
    the diff guard) until the pin is updated deliberately after an ADR 0014
    re-review.
    """
    assert set(RecognitionResultDTO.model_fields) == EXPECTED_RECOGNITION_RESULT_FIELDS
    assert set(SessionSummaryDTO.model_fields) == EXPECTED_SESSION_SUMMARY_FIELDS
    assert set(RecognizedEventDTO.model_fields) == EXPECTED_RECOGNIZED_EVENT_FIELDS
    assert set(RelatedEntityDTO.model_fields) == EXPECTED_RELATED_ENTITY_FIELDS


def test_image_and_video_mode_produce_identical_dto() -> None:
    """The same JSON parses to byte-identical DTOs in both payload modes.

    The parser is shared by design (ADR 0014: both modes parse into the
    same ``RecognitionResultDTO``), so invariance is structural; the
    end-to-end run through :func:`execute_sub_chunk` proves the payload
    mode (frames vs raw_mp4) never alters the output.
    """
    video_dto = parse_video_recognition_output(FIXED_RECOGNITION_JSON)
    image_dto = parse_video_recognition_output(FIXED_RECOGNITION_JSON)
    assert video_dto.model_dump() == image_dto.model_dump()

    video_result = _run_execute(frames=None)
    image_result = _run_execute(frames=_frames())
    assert video_result.payload_mode == "video"
    assert image_result.payload_mode == "image"
    assert (
        video_result.recognition_result.model_dump()
        == image_result.recognition_result.model_dump()
        == video_dto.model_dump()
    )


# ---------------------------------------------------------------------------
# 2. Offset mapping: base_offset + offset == absolute session time
# ---------------------------------------------------------------------------


def test_offset_mapping_yields_absolute_session_time() -> None:
    """With ``base_offset_seconds=120`` the two sample events (0..5, 55..60)
    map to absolute offsets 120..125 and 175..180, i.e.
    ``session_start_time + base_offset + offset`` — identical for the DTO
    produced in image mode and in video mode (same DTO, mode-agnostic
    mapper).
    """
    dto = parse_video_recognition_output(FIXED_RECOGNITION_JSON)
    base_offset = 120
    session = _session()
    records = [
        build_event_record_from_recognized_event(session, event, base_offset_seconds=base_offset)
        for event in dto.events
    ]

    start = session.session_start_time
    assert float(records[0].offset_start_sec) == 120.0
    assert float(records[0].offset_end_sec) == 125.0
    assert records[0].event_start_time == start + timedelta(seconds=120)
    assert records[0].event_end_time == start + timedelta(seconds=125)
    assert float(records[1].offset_start_sec) == 175.0
    assert float(records[1].offset_end_sec) == 180.0
    assert records[1].event_start_time == start + timedelta(seconds=175)
    assert records[1].event_end_time == start + timedelta(seconds=180)

    # Invariance across modes: re-parse (the image-mode path) and map again;
    # the mapper is mode-agnostic, so the outputs must be identical.
    dto_image = parse_video_recognition_output(FIXED_RECOGNITION_JSON)
    records_image = [
        build_event_record_from_recognized_event(session, event, base_offset_seconds=base_offset)
        for event in dto_image.events
    ]
    for record, record_image in zip(records, records_image, strict=True):
        assert record.offset_start_sec == record_image.offset_start_sec
        assert record.offset_end_sec == record_image.offset_end_sec
        assert record.event_start_time == record_image.event_start_time
        assert record.event_end_time == record_image.event_end_time


# ---------------------------------------------------------------------------
# 3. Rejection: offset_end_sec beyond the sub-chunk duration
# ---------------------------------------------------------------------------


def test_event_offset_beyond_sub_chunk_duration_is_rejected() -> None:
    """An event whose ``offset_end_sec`` exceeds the sub-chunk duration is
    rejected with a locatable error naming both values.

    The schema/parser alone cannot reject it (they do not know the
    duration), so the boundary guard
    :func:`validate_recognition_offsets_within_sub_chunk` (added for this
    wave, invoked by the analyzer orchestration right after parsing) is
    the guard; the test documents both facts.
    """
    out_of_bounds = RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text="summary",
            activity_level="medium",
            main_subjects=[],
        ),
        events=[
            RecognizedEventDTO(
                offset_start_sec=85,
                offset_end_sec=90,
                event_type="member_appear",
                title="t",
                summary="s",
                detail="d",
                related_entities=[],
                observed_actions=[],
                interpreted_state=[],
                confidence=0.9,
            )
        ],
        analysis_notes=[],
    )

    with pytest.raises(RecognitionOutputValidationError) as excinfo:
        validate_recognition_offsets_within_sub_chunk(out_of_bounds, sub_chunk_duration_seconds=60)
    message = str(excinfo.value)
    assert "offset_end_sec=90" in message
    assert "duration_seconds=60" in message
    assert "event #0" in message

    # The sample's events (0..5, 55..60) sit exactly inside a 60s window
    # (offset_end_sec == duration is allowed, not "beyond") — no rejection.
    in_bounds = parse_video_recognition_output(FIXED_RECOGNITION_JSON)
    validate_recognition_offsets_within_sub_chunk(in_bounds, sub_chunk_duration_seconds=60)

    # Pre-existing behaviour: the parser alone accepts out-of-window offsets
    # (it cannot know the duration), so the DTO builds without error.
    out_of_bounds_json = FIXED_RECOGNITION_JSON.replace(
        '"offset_end_sec": 60', '"offset_end_sec": 90'
    )
    parsed = parse_video_recognition_output(out_of_bounds_json)
    assert parsed.events[1].offset_end_sec == 90.0
    with pytest.raises(RecognitionOutputValidationError):
        validate_recognition_offsets_within_sub_chunk(parsed, sub_chunk_duration_seconds=60)


# ---------------------------------------------------------------------------
# 4. finalize/replace path carries absolute base_offset + offset
# ---------------------------------------------------------------------------


def test_finalized_events_carry_absolute_base_offset(pg_db: Session) -> None:
    """Through the real finalize path (``completed_results_for_run`` +
    ``replace_session_events``), a checkpoint with
    ``start_offset_seconds=120`` produces persisted events at absolute
    offsets 120..125 and 175..180.
    """
    now = datetime(2026, 3, 14, 10, 0, 0, tzinfo=timezone.utc)
    source = VideoSource(
        source_name="contract-source",
        camera_name="cam",
        location_name="living-room",
        source_type="local_directory",
    )
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=now,
        session_end_time=now + timedelta(minutes=10),
        total_duration_seconds=600,
        analysis_status="success",
    )
    pg_db.add(session)
    pg_db.flush()

    dto = parse_video_recognition_output(FIXED_RECOGNITION_JSON)
    checkpoint = SessionAnalysisCheckpoint(
        session_id=session.id,
        analysis_run_id="run-1",
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=120,
        input_fingerprint="frames:invariance",
        state="success",
        event_payload=dto.model_dump(mode="json"),
    )
    pg_db.add(checkpoint)
    pg_db.flush()

    results, events = completed_results_for_run(pg_db, session, "run-1")
    assert len(results) == 1
    assert results[0][0] == 0 and results[0][1] == 0
    assert len(events) == 2

    assert float(events[0].offset_start_sec) == 120.0
    assert float(events[0].offset_end_sec) == 125.0
    assert events[0].event_start_time == now + timedelta(seconds=120)
    assert events[0].event_end_time == now + timedelta(seconds=125)
    assert float(events[1].offset_start_sec) == 175.0
    assert float(events[1].offset_end_sec) == 180.0
    assert events[1].event_start_time == now + timedelta(seconds=175)
    assert events[1].event_end_time == now + timedelta(seconds=180)

    replace_session_events(pg_db, session.id, events)
    pg_db.flush()
    stored = (
        pg_db.query(EventRecord)
        .filter(EventRecord.session_id == session.id)
        .order_by(EventRecord.offset_start_sec)
        .all()
    )
    assert len(stored) == 2
    assert [float(event.offset_start_sec) for event in stored] == [120.0, 175.0]
    assert [float(event.offset_end_sec) for event in stored] == [125.0, 180.0]
    assert stored[0].event_start_time == now + timedelta(seconds=120)
    assert stored[1].event_end_time == now + timedelta(seconds=180)


# ---------------------------------------------------------------------------
# 5. Diff guard on schemas.py
# ---------------------------------------------------------------------------


def _normalized_source(path: Path) -> str:
    """Normalize the source text before hashing: rstrip each line and drop
    trailing blank lines, so whitespace-only churn does not trip the guard
    while any semantic edit still changes the digest."""
    text = path.read_text(encoding="utf-8")
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def test_schemas_py_diff_guard() -> None:
    """Any edit to schemas.py fails this test.

    The pin is intentional: it forces a human to re-verify that the
    image-mode output contract (ADR 0014) is still invariant — i.e. the
    single shared ``RecognitionResultDTO`` is still the exact downstream
    shape for both payload modes — before updating
    ``PINNED_SCHEMAS_SHA256`` deliberately.
    """
    actual = sha256(_normalized_source(SCHEMAS_PATH).encode("utf-8")).hexdigest()
    assert actual == PINNED_SCHEMAS_SHA256, (
        "src/services/video_analysis/schemas.py changed (actual "
        f"sha256 {actual} != pinned {PINNED_SCHEMAS_SHA256}). The image-mode "
        "output contract (ADR 0014) may no longer be invariant: re-verify that "
        "RecognitionResultDTO is still the single shared output shape for both "
        "payload modes, re-run this suite, and update PINNED_SCHEMAS_SHA256 "
        "deliberately."
    )
