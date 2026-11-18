"""Unit tests for the frame-set checkpoint fingerprint (Wave B todo 11).

The sub-chunk checkpoint fingerprint no longer hashes the raw video
payload directly: the caller supplies a *namespaced* frame fingerprint
(``"frames:" + frame_set_fingerprint(...)`` for image mode,
``"video:" + sha256(video_data_url)`` for video mode) and
:func:`src.services.analysis.plan.sub_chunk_fingerprint` combines it
with the prompts, offsets and file paths. The two namespaces are
provably distinct, so a payload-mode switch (or any change behind the
namespaced value — frames, policy, source content) invalidates stored
checkpoints.

The image-mode behaviour proof drives
:func:`src.services.analysis.execute.execute_sub_chunk` with a
:class:`~src.application.bootstrap_fakes.ScriptedVisionGateway` — the
same fake as ``test_analyzer_image_payload.py`` — so no network call
happens.
"""

from __future__ import annotations

import base64
import re
from hashlib import sha256
from typing import Any

import pytest

from src.application.bootstrap_fakes import ScriptedVisionGateway
from src.models.llm_provider import LLMProvider
from src.services.analysis.execute import (
    FRAMES_FINGERPRINT_PREFIX,
    VIDEO_FINGERPRINT_PREFIX,
    SubChunkRunResult,
    execute_sub_chunk,
    frames_mode_frame_fingerprint,
    video_mode_frame_fingerprint,
)
from src.services.analysis.plan import PLAN_VERSION, SubChunkPlan, sub_chunk_fingerprint
from src.services.frame_sampling.fingerprint import frame_set_fingerprint
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import FrameSample, FrameSet
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
USER_PROMPT = "请逐帧描述画面"
VIDEO_DATA_URL = "data:video/mp4;base64,AAAA"
CONTENT_SIGNATURE = "content-signature"


def _sub_chunk(**overrides: Any) -> SubChunkPlan:
    base: dict[str, Any] = {
        "chunk_index": 0,
        "sub_chunk_index": 0,
        "start_offset_seconds": 0,
        "duration_seconds": 60,
        "file_paths": ("/tmp/fake.mp4",),
    }
    base.update(overrides)
    return SubChunkPlan(**base)


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


def _provider() -> LLMProvider:
    """A lightweight in-memory provider (no DB)."""
    return LLMProvider(
        provider_name="fingerprint-test",
        provider_type="vision_provider",
        api_base_url="http://example.invalid/v1",
        api_key="",
        model_name="vision-test",
        timeout_seconds=30,
        enabled=True,
        supports_vision=True,
        is_default_vision=True,
    )


def _three_frames() -> list[FrameSample]:
    return [
        FrameSample(t_seconds=0.0, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-1", reason="uniform"),
        FrameSample(t_seconds=3.4, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-2", reason="motion"),
        FrameSample(t_seconds=7.8, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-3", reason="uniform"),
    ]


# ---------------------------------------------------------------------------
# sub_chunk_fingerprint — determinism and sensitivity
# ---------------------------------------------------------------------------


def test_sub_chunk_fingerprint_is_deterministic_and_64_hex_chars() -> None:
    """Two calls with identical inputs produce the same 64-char hex digest."""
    sub_chunk = _sub_chunk()
    first = sub_chunk_fingerprint(
        system_prompt="sys",
        user_prompt="user",
        frame_fingerprint="frames:deadbeef",
        sub_chunk=sub_chunk,
    )
    second = sub_chunk_fingerprint(
        system_prompt="sys",
        user_prompt="user",
        frame_fingerprint="frames:deadbeef",
        sub_chunk=sub_chunk,
    )
    assert first == second
    assert HEX64_RE.match(first) is not None


@pytest.mark.parametrize(
    ("sub_chunk_overrides", "kwargs_overrides"),
    [
        ({}, {"frame_fingerprint": "frames:changed"}),
        ({}, {"system_prompt": "sys-changed"}),
        ({}, {"user_prompt": "user-changed"}),
        ({"start_offset_seconds": 60}, {}),
        ({"duration_seconds": 120}, {}),
        ({"file_paths": ("/tmp/other.mp4",)}, {}),
    ],
    ids=[
        "frame_fingerprint",
        "system_prompt",
        "user_prompt",
        "start_offset_seconds",
        "duration_seconds",
        "file_paths",
    ],
)
def test_sub_chunk_fingerprint_changes_when_any_input_changes(
    sub_chunk_overrides: dict[str, Any],
    kwargs_overrides: dict[str, Any],
) -> None:
    """Changing exactly one input — frame fingerprint, prompts, offsets
    or file paths — changes the fingerprint."""
    base_kwargs: dict[str, Any] = {
        "system_prompt": "sys",
        "user_prompt": "user",
        "frame_fingerprint": "frames:deadbeef",
        "sub_chunk": _sub_chunk(),
    }
    reference = sub_chunk_fingerprint(**base_kwargs)

    changed_kwargs = dict(base_kwargs)
    changed_kwargs.update(kwargs_overrides)
    if sub_chunk_overrides:
        changed_kwargs["sub_chunk"] = _sub_chunk(**sub_chunk_overrides)

    assert sub_chunk_fingerprint(**changed_kwargs) != reference


def test_plan_version_is_two() -> None:
    assert PLAN_VERSION == 2


# ---------------------------------------------------------------------------
# Cross-mode distinctness: video: vs frames: namespaces
# ---------------------------------------------------------------------------


def test_video_mode_and_frames_mode_fingerprints_are_distinct() -> None:
    """Over the SAME underlying bytes, a video-mode input
    (``"video:" + sha256(payload)``) and an image-mode input
    (``"frames:" + frame_set_fingerprint(...)``) never produce equal
    sub-chunk fingerprints — the distinct prefixes make the values
    provably distinct.
    """
    payload = b"\x00\x01\x02\x03"
    video_url = "data:video/mp4;base64," + base64.b64encode(payload).decode()
    video_mode = video_mode_frame_fingerprint(video_url)

    frames = (FrameSample(0.0, payload, "uniform"),)
    frames_mode = frames_mode_frame_fingerprint(
        frames,
        FramePolicy(),
        content_signature=sha256(payload).hexdigest(),
    )

    assert video_mode.startswith(VIDEO_FINGERPRINT_PREFIX)
    assert frames_mode.startswith(FRAMES_FINGERPRINT_PREFIX)
    assert video_mode != frames_mode

    sub_chunk = _sub_chunk()
    video_fingerprint = sub_chunk_fingerprint(
        system_prompt="sys",
        user_prompt="user",
        frame_fingerprint=video_mode,
        sub_chunk=sub_chunk,
    )
    frames_fingerprint = sub_chunk_fingerprint(
        system_prompt="sys",
        user_prompt="user",
        frame_fingerprint=frames_mode,
        sub_chunk=sub_chunk,
    )
    assert video_fingerprint != frames_fingerprint


# ---------------------------------------------------------------------------
# Policy change is a checkpoint-reset trigger
# ---------------------------------------------------------------------------


def test_policy_change_changes_frame_set_fingerprint() -> None:
    """Same frames, different policy -> different frame_set_fingerprint,
    and the fingerprint fed to sub_chunk_fingerprint differs (this is the
    checkpoint-reset trigger)."""
    frames = (FrameSample(0.0, b"\xff\xd8\xff\xe0JPEG"),)
    policy_a = FramePolicy(min_frames=4, max_frames=120)
    policy_b = FramePolicy(min_frames=8, max_frames=120)

    set_a = frame_set_fingerprint(frames, policy_a, content_signature=CONTENT_SIGNATURE)
    set_b = frame_set_fingerprint(frames, policy_b, content_signature=CONTENT_SIGNATURE)
    assert set_a != set_b

    sub_chunk = _sub_chunk()
    fed_a = sub_chunk_fingerprint(
        system_prompt="sys",
        user_prompt="user",
        frame_fingerprint=f"{FRAMES_FINGERPRINT_PREFIX}{set_a}",
        sub_chunk=sub_chunk,
    )
    fed_b = sub_chunk_fingerprint(
        system_prompt="sys",
        user_prompt="user",
        frame_fingerprint=f"{FRAMES_FINGERPRINT_PREFIX}{set_b}",
        sub_chunk=sub_chunk,
    )
    assert fed_a != fed_b


# ---------------------------------------------------------------------------
# execute_sub_chunk — behaviour proof for image mode (fake gateway, no network)
# ---------------------------------------------------------------------------


def _run_execute(
    *,
    frames: Any,
    frame_fingerprint: str | None = None,
) -> SubChunkRunResult:
    client = ScriptedVisionGateway(responses=[_recognition_result_json()])
    return execute_sub_chunk(
        client=client,
        provider=_provider(),
        sub_chunk=_sub_chunk(),
        system_prompt="system",
        user_prompt=USER_PROMPT,
        video_data_url=VIDEO_DATA_URL,
        frames=frames,
        frame_fingerprint=frame_fingerprint,
    )


def test_identical_frame_set_produces_same_execute_checkpoint_fingerprint() -> None:
    """Two runs over an identical FrameSet (same policy) produce the same
    execute_sub_chunk checkpoint fingerprint."""
    frame_set = FrameSet(frames=tuple(_three_frames()))
    policy = FramePolicy()

    first = _run_execute(
        frames=frame_set.frames,
        frame_fingerprint=frames_mode_frame_fingerprint(
            frame_set, policy, content_signature=CONTENT_SIGNATURE
        ),
    )
    second = _run_execute(
        frames=frame_set.frames,
        frame_fingerprint=frames_mode_frame_fingerprint(
            frame_set, policy, content_signature=CONTENT_SIGNATURE
        ),
    )

    assert first.frame_fingerprint == second.frame_fingerprint
    assert first.frame_fingerprint.startswith(FRAMES_FINGERPRINT_PREFIX)
    assert first.payload_mode == "image"


def test_changed_policy_produces_different_execute_checkpoint_fingerprint() -> None:
    """Same FrameSet, changed policy -> different execute_sub_chunk
    checkpoint fingerprint (the checkpoint-reset trigger at the
    execute seam)."""
    frame_set = FrameSet(frames=tuple(_three_frames()))

    first = _run_execute(
        frames=frame_set.frames,
        frame_fingerprint=frames_mode_frame_fingerprint(
            frame_set,
            FramePolicy(min_frames=4, max_frames=120),
            content_signature=CONTENT_SIGNATURE,
        ),
    )
    second = _run_execute(
        frames=frame_set.frames,
        frame_fingerprint=frames_mode_frame_fingerprint(
            frame_set,
            FramePolicy(min_frames=8, max_frames=120),
            content_signature=CONTENT_SIGNATURE,
        ),
    )

    assert first.frame_fingerprint != second.frame_fingerprint
    assert first.payload_mode == "image"
    assert second.payload_mode == "image"


def test_video_mode_defaults_to_namespaced_video_fingerprint() -> None:
    """Without a caller-supplied frame fingerprint, execute_sub_chunk
    computes the legacy namespaced video value:
    ``"video:" + sha256(video_data_url)`` — today's invalidation
    semantics with a namespaced stored value."""
    result = _run_execute(frames=None)

    expected = f"{VIDEO_FINGERPRINT_PREFIX}{sha256(VIDEO_DATA_URL.encode()).hexdigest()}"
    assert result.frame_fingerprint == expected
    assert result.frame_fingerprint == video_mode_frame_fingerprint(VIDEO_DATA_URL)
    assert result.payload_mode == "video"
