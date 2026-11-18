"""Unit tests for the provider TEST frame-plan preview (Wave E / todo 26).

The provider test endpoint must report a frame-plan preview (frames /
tokens_per_image / image_tokens / budget_tokens / clamp_reason /
max_output_tokens) for image-mode providers. The preview is computed from the
provider config through the same helper the create / update validation uses,
so it is independent of reachability (present even when the probe fails) and
absent (``None``, never a fabricated empty dict) for video-mode providers or
unbuildable plans.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.application.bootstrap_fakes import FakeLLMGateway, FakeLLMGatewayFactory
from src.application.llm_providers import test_provider_use_case as run_test_provider_use_case
from src.core.provider_key_crypto import encrypt_provider_api_key
from src.services.llm_provider_tester import ProviderTestResult, check_provider_connectivity

FRAME_PLAN_KEYS = {
    "frames",
    "tokens_per_image",
    "image_tokens",
    "budget_tokens",
    "clamp_reason",
    "max_output_tokens",
}


def _provider_stub(extra_config_json: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        api_base_url="http://localhost:8000/v1",
        api_key=encrypt_provider_api_key("test-key"),
        model_name="test-model",
        timeout_seconds=30,
        extra_config_json=extra_config_json,
    )


def _reachable_factory() -> FakeLLMGatewayFactory:
    factory = FakeLLMGatewayFactory()

    def _build_with_reply(**kwargs):
        gateway = FakeLLMGateway(model_name=kwargs.get("model_name", "test-model"))
        gateway.replies.append("pong")
        factory.gateways.append(gateway)
        factory.build_calls.append(kwargs)
        return gateway

    factory.build = _build_with_reply  # type: ignore[assignment]
    return factory


def test_image_provider_reachable_returns_frame_plan_preview() -> None:
    provider = _provider_stub({"max_model_len": 65536, "frame_sampling": {}})

    result = check_provider_connectivity(provider, llm_factory=_reachable_factory())

    assert isinstance(result, ProviderTestResult)
    assert result.success is True
    assert result.frame_plan is not None
    assert set(result.frame_plan) == FRAME_PLAN_KEYS
    assert result.frame_plan["frames"] > 0
    assert (
        result.frame_plan["image_tokens"]
        == result.frame_plan["frames"] * result.frame_plan["tokens_per_image"]
    )
    # matches the plan the create / update validation computes (65536 ctx,
    # 60 s representative clip): max_frames ceiling bounds the budget
    assert result.frame_plan["frames"] == 120
    assert result.frame_plan["tokens_per_image"] == 300
    assert result.frame_plan["max_output_tokens"] == 4096
    assert result.frame_plan["clamp_reason"] in {"budget", "max", "duration"}


def test_image_provider_unreachable_still_returns_frame_plan() -> None:
    provider = _provider_stub({"max_model_len": 65536, "frame_sampling": {}})

    with patch.object(FakeLLMGateway, "chat_completion", side_effect=ConnectionError("refused")):
        result = check_provider_connectivity(provider, llm_factory=_reachable_factory())

    assert result.success is False
    assert "refused" in result.message
    assert result.frame_plan is not None
    assert set(result.frame_plan) == FRAME_PLAN_KEYS
    assert result.frame_plan["frames"] > 0
    assert (
        result.frame_plan["image_tokens"]
        == result.frame_plan["frames"] * result.frame_plan["tokens_per_image"]
    )


def test_image_provider_impossible_config_has_no_frame_plan() -> None:
    provider = _provider_stub(
        {"max_model_len": 8192, "frame_sampling": {"max_output_tokens": 8192, "min_frames": 8}}
    )
    factory = FakeLLMGatewayFactory()

    result = check_provider_connectivity(provider, llm_factory=factory)

    assert result.success is False
    # the message names the three remedies
    assert "raise max_model_len" in result.message
    assert "lower max_output_tokens" in result.message
    assert "lower min_frames" in result.message
    # no fabricated empty dict: the preview is None on the error path
    assert result.frame_plan is None
    # fail fast: no network round-trip
    assert factory.gateways == []


def test_video_provider_frame_plan_is_none() -> None:
    provider = _provider_stub(None)

    result = check_provider_connectivity(provider, llm_factory=_reachable_factory())

    assert result.success is True
    assert result.frame_plan is None
    # the rest of the result is unchanged from today
    assert result.supports_vision is False
    assert result.supports_tool_calling is False
    assert "pong" not in result.message


class _FakeQuery:
    def __init__(self, provider: object) -> None:
        self._provider = provider

    def filter(self, *args: object) -> _FakeQuery:
        return self

    def first(self) -> object:
        return self._provider


class _FakeDB:
    def __init__(self, provider: object) -> None:
        self._provider = provider

    def query(self, *args: object) -> _FakeQuery:
        return _FakeQuery(self._provider)

    def commit(self) -> None:
        pass


def test_use_case_payload_carries_frame_plan() -> None:
    provider = _provider_stub({"max_model_len": 65536, "frame_sampling": {}})
    container = SimpleNamespace(llm_factory=_reachable_factory())

    result = run_test_provider_use_case(_FakeDB(provider), 1, "zh-CN", container)

    assert result.error_code == 0
    assert result.payload is not None
    assert result.payload["frame_plan"] is not None
    assert set(result.payload["frame_plan"]) == FRAME_PLAN_KEYS
    assert (
        result.payload["frame_plan"]["image_tokens"]
        == result.payload["frame_plan"]["frames"] * result.payload["frame_plan"]["tokens_per_image"]
    )
    assert provider.last_test_status == "success"
