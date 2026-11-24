"""One-shot request extras (``strata_checkpoint``) and their call-site wiring."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

from src.models.llm_provider import LLMProvider
from src.services.analysis.execute import execute_sub_chunk, frames_mode_frame_fingerprint
from src.services.analysis.plan import SubChunkPlan
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import FrameSample
from src.services.provider_request_flags import resolve_one_shot_request_extras
from src.services.summarizer.generation import _chat
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)


class _RecordingGateway:
    """Minimal gateway fake that records every ``chat_completion`` kwarg."""

    supports_tool_calling = False

    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def chat_completion(self, messages, **kwargs: Any) -> str:
        self.calls.append({"messages": messages, **kwargs})
        return self.response

    def chat_completion_with_tools(self, messages, tools, **kwargs: Any):
        return None, None

    def get_last_usage(self) -> dict[str, int] | None:
        return None

    def get_last_raw_response_text(self) -> str | None:
        return self.response

    def close(self) -> None:
        return None

    def probe_vision(self) -> bool:
        return True

    def probe_tool_calling(self) -> bool:
        return False


def _provider(extra_config_json: Any = None) -> LLMProvider:
    return LLMProvider(
        provider_name="strata-test",
        provider_type="vision_provider",
        api_base_url="http://example.invalid/v1",
        api_key="",
        model_name="qwen3.5-9b",
        timeout_seconds=30,
        enabled=True,
        supports_vision=True,
        is_default_vision=True,
        extra_config_json=extra_config_json,
    )


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


def _frames() -> list[FrameSample]:
    return [
        FrameSample(t_seconds=0.0, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-1", reason="uniform"),
        FrameSample(t_seconds=3.5, jpeg_bytes=b"\xff\xd8\xff\xe0JPEG-2", reason="motion"),
    ]


def _sub_chunk() -> SubChunkPlan:
    return SubChunkPlan(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=("/tmp/fake.mp4",),
    )


# ---------------------------------------------------------------------------
# Resolver: opt-in only, exact boolean False
# ---------------------------------------------------------------------------


def test_default_provider_does_not_send_the_field() -> None:
    assert resolve_one_shot_request_extras(_provider(None)) == {}
    assert resolve_one_shot_request_extras(_provider({})) == {}
    assert resolve_one_shot_request_extras(_provider({"frame_sampling": {}})) == {}


def test_truthy_or_malformed_values_do_not_send_the_field() -> None:
    # A typo ("false" as a string) must not silently emit the field: only the
    # exact JSON boolean is accepted.
    assert resolve_one_shot_request_extras(_provider({"strata_checkpoint": True})) == {}
    assert resolve_one_shot_request_extras(_provider({"strata_checkpoint": "false"})) == {}
    assert resolve_one_shot_request_extras(_provider({"strata_checkpoint": 0})) == {}


def test_explicit_false_emits_the_exact_wire_field() -> None:
    assert resolve_one_shot_request_extras(_provider({"strata_checkpoint": False})) == {
        "strata_checkpoint": False
    }


# ---------------------------------------------------------------------------
# Call-site wiring: image mode must not drop caller extras
# ---------------------------------------------------------------------------


def test_image_mode_vision_call_carries_strata_checkpoint() -> None:
    gateway = _RecordingGateway(response=_recognition_result_json())
    frames = _frames()
    provider = _provider({"strata_checkpoint": False})

    result = execute_sub_chunk(
        client=gateway,
        provider=provider,
        sub_chunk=_sub_chunk(),
        system_prompt="system",
        user_prompt="user",
        frames=frames,
        frame_fingerprint=frames_mode_frame_fingerprint(
            frames, FramePolicy(), content_signature="unit-test"
        ),
        extra_body=resolve_one_shot_request_extras(provider),
    )

    assert gateway.calls[0]["extra_body"] == {"strata_checkpoint": False}
    assert result.payload_mode == "image"
    assert "media_io_kwargs" not in result.extra_body


def test_image_mode_without_flag_keeps_empty_extra_body() -> None:
    gateway = _RecordingGateway(response=_recognition_result_json())
    frames = _frames()

    result = execute_sub_chunk(
        client=gateway,
        provider=_provider({}),
        sub_chunk=_sub_chunk(),
        system_prompt="system",
        user_prompt="user",
        frames=frames,
        frame_fingerprint=frames_mode_frame_fingerprint(
            frames, FramePolicy(), content_signature="unit-test"
        ),
        extra_body=resolve_one_shot_request_extras(_provider({})),
    )

    assert gateway.calls[0]["extra_body"] == {}
    assert result.extra_body == {}


def test_daily_summary_chat_carries_the_extras() -> None:
    gateway = _RecordingGateway(response="{}")

    _chat(
        db=None,
        client=gateway,
        provider_id=1,
        provider_name_snapshot="strata-test",
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"strata_checkpoint": False},
    )

    assert gateway.calls[0]["extra_body"] == {"strata_checkpoint": False}


def test_daily_summary_orchestration_derives_the_flag_from_the_provider(monkeypatch) -> None:
    from src.tasks import _summarizer_orchestration as mod

    captured: dict[str, Any] = {}

    def fake_single_pass(**kwargs: Any):
        captured.update(kwargs)
        return "overall", [], [], 0, False

    monkeypatch.setattr(mod, "generate_single_pass_summary_payload", fake_single_pass)
    provider = SimpleNamespace(
        id=1,
        provider_name="strata-test",
        extra_config_json={"strata_checkpoint": False},
    )
    evidence = SimpleNamespace(
        single_pass_user_prompt_chars=10,
        events=[object()],
        prompt=("system", "user"),
        home_context={},
        known_subjects=[],
        subject_sections=[],
        missing_subjects=[],
        attention_candidates=[],
        time_range_start="",
        time_range_end="",
    )

    mod._run_llm_phase(
        db=None,
        client=None,
        provider=provider,
        target_date=date(2026, 3, 13),
        evidence=evidence,
        locale=None,
        serial_split_prompt_threshold=100,
    )

    assert captured["request_extra_body"] == {"strata_checkpoint": False}
