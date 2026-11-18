"""Unit tests for the interleaved image-frame payload in the analysis execute path.

Wave B (todo 8) of the image-frame-sampling plan: when the frame sampler
returns frames, the execute path must send an interleaved ``text``/
``image_url`` user payload (``data:image/jpeg;base64,...``) with an EMPTY
``extra_body`` (no ``media_io_kwargs``). When no frames are supplied
(``None`` or an empty sequence), the existing raw_mp4 video payload is kept
byte-for-byte.

The tests drive the public :func:`src.services.analysis.execute.execute_sub_chunk`
seam with a :class:`~src.application.bootstrap_fakes.ScriptedVisionGateway` —
the same fake the raw_mp4 regression test (``test_analyzer_raw_mp4_payload.py``)
uses — so no network call happens; every assertion reads the gateway's
recorded ``calls`` log (full kwargs, including the message content and the
``extra_body``).
"""

from __future__ import annotations

import base64
from typing import Any

from src.application.bootstrap_fakes import ScriptedVisionGateway
from src.models.llm_provider import LLMProvider
from src.services.analysis.execute import (
    SubChunkRunResult,
    execute_sub_chunk,
    frames_mode_frame_fingerprint,
)
from src.services.analysis.plan import SubChunkPlan
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import FrameSample
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)

USER_PROMPT = "请逐帧描述画面"
VIDEO_DATA_URL = "data:video/mp4;base64,AAAA"


def _recognition_result_json() -> str:
    """A minimal valid LLM JSON payload that parses into a DTO."""
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


def _provider(**extra: Any) -> LLMProvider:
    """A lightweight in-memory provider (no DB).

    ``extra_config_json`` defaults to ``None`` so ``resolve_num_frames`` falls
    back to the caller's scene default (120 for the raw_mp4 hint).
    """
    return LLMProvider(
        provider_name="image-payload-test",
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


def _three_frames() -> list[FrameSample]:
    """Three frames with distinct JPEG payloads and non-integer timestamps.

    Distinct payloads let the base64 round-trip assertion catch any frame
    mix-up; the non-integer timestamps pin the one-decimal label formatting.
    """
    return [
        FrameSample(t_seconds=0.0, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-1", reason="uniform"),
        FrameSample(t_seconds=3.4, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-2", reason="motion"),
        FrameSample(t_seconds=7.8, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-3", reason="uniform"),
    ]


def _run_execute(
    *,
    frames: Any,
    provider: LLMProvider | None = None,
) -> tuple[ScriptedVisionGateway, SubChunkRunResult]:
    client = ScriptedVisionGateway(responses=[_recognition_result_json()])
    frame_fingerprint = (
        frames_mode_frame_fingerprint(frames, FramePolicy(), content_signature="unit-test")
        if frames
        else None
    )
    result = execute_sub_chunk(
        client=client,
        provider=provider or _provider(),
        sub_chunk=_sub_chunk(),
        system_prompt="system",
        user_prompt=USER_PROMPT,
        video_data_url=VIDEO_DATA_URL,
        frames=frames,
        frame_fingerprint=frame_fingerprint,
    )
    return client, result


def test_image_mode_builds_interleaved_payload() -> None:
    """N=3 frames produce exactly ``2*N + 1 == 7`` user-content parts.

    Layout is label, image, ..., label, image, then the user prompt; every
    image url decodes back to the frame's JPEG bytes; labels carry the exact
    one-decimal timestamp; the last part is the user prompt verbatim; the
    result is image mode with an empty ``extra_body``.
    """
    frames = _three_frames()
    client, result = _run_execute(frames=frames)

    call = client.calls[0]
    content = call["messages"][1]["content"]

    assert len(content) == 7
    assert [item["type"] for item in content] == [
        "text",
        "image_url",
        "text",
        "image_url",
        "text",
        "image_url",
        "text",
    ]
    # First element is the first frame's label, one decimal second.
    assert content[0]["text"] == "帧 1/3 — t=0.0s"
    # Every label carries the exact timestamp of its frame.
    assert content[2]["text"] == "帧 2/3 — t=3.4s"
    assert content[4]["text"] == "帧 3/3 — t=7.8s"
    # Each image url decodes back to the same jpeg bytes (base64 round-trip).
    for index, frame in enumerate(frames):
        url = content[2 * index + 1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == frame.jpeg_bytes
    # The last element is the user prompt verbatim.
    assert content[-1]["text"] == USER_PROMPT

    assert result.payload_mode == "image"
    assert result.extra_body == {}


def test_video_mode_keeps_raw_mp4_payload() -> None:
    """``frames=None`` keeps today's exact video payload (regression pin).

    Minimal copy of the raw_mp4 payload assertions so a regression in the
    video path fails here too: content length 2, first element ``video_url``,
    ``payload_mode == "video"``, and the ``num_frames`` hint defaults to 120
    for a provider with no ``num_frames`` configured.
    """
    client, result = _run_execute(frames=None)

    call = client.calls[0]
    content = call["messages"][1]["content"]

    assert len(content) == 2
    assert content[0]["type"] == "video_url"
    assert content[0]["video_url"]["url"] == VIDEO_DATA_URL
    assert content[1]["type"] == "text"
    assert content[1]["text"] == USER_PROMPT

    assert result.payload_mode == "video"
    assert call["extra_body"]["media_io_kwargs"]["video"]["num_frames"] == 120
    assert result.extra_body == {"media_io_kwargs": {"video": {"num_frames": 120}}}


def test_empty_frames_behaves_like_video_mode() -> None:
    """An empty frames sequence is backward-compatible with video mode.

    ``frames=()`` must produce the identical payload to ``frames=None`` —
    the video_url part, the text part, the ``num_frames`` hint, and
    ``payload_mode == "video"``.
    """
    client, result = _run_execute(frames=())

    call = client.calls[0]
    content = call["messages"][1]["content"]

    assert len(content) == 2
    assert content[0]["type"] == "video_url"
    assert content[1]["type"] == "text"
    assert content[1]["text"] == USER_PROMPT

    assert result.payload_mode == "video"
    assert call["extra_body"]["media_io_kwargs"]["video"]["num_frames"] == 120
    assert result.extra_body == {"media_io_kwargs": {"video": {"num_frames": 120}}}
