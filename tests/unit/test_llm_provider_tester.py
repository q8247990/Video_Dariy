from unittest.mock import patch

import pytest

from src.application.bootstrap_fakes import FakeLLMGateway, FakeLLMGatewayFactory
from src.services.llm_provider_tester import ProviderTestResult, check_provider_connectivity
from src.services.provider_key_crypto import encrypt_provider_api_key


def _make_provider():
    class _Provider:
        api_base_url = "http://localhost:8000/v1"
        api_key = encrypt_provider_api_key("test-key")
        model_name = "test-model"
        timeout_seconds = 30

    return _Provider()


def test_success_with_vision_and_tool_calling() -> None:
    factory = FakeLLMGatewayFactory()

    def _build_with_reply(**kwargs):
        gateway = FakeLLMGateway(model_name=kwargs.get("model_name", "test-model"))
        gateway.replies.append("pong")
        factory.gateways.append(gateway)
        factory.build_calls.append(kwargs)
        return gateway

    factory.build = _build_with_reply  # type: ignore[assignment]

    with (
        patch.object(FakeLLMGateway, "probe_vision", return_value=True),
        patch.object(FakeLLMGateway, "probe_tool_calling", return_value=True),
    ):
        result = check_provider_connectivity(_make_provider(), llm_factory=factory)

    assert isinstance(result, ProviderTestResult)
    assert result.success is True
    assert result.supports_vision is True
    assert result.supports_tool_calling is True
    assert "视觉" in result.message
    assert "工具调用" in result.message
    assert factory.gateways[0]._closed is True


def test_success_no_capabilities() -> None:
    factory = FakeLLMGatewayFactory()

    def _build_with_reply(**kwargs):
        gateway = FakeLLMGateway(model_name=kwargs.get("model_name", "test-model"))
        gateway.replies.append("pong")
        factory.gateways.append(gateway)
        factory.build_calls.append(kwargs)
        return gateway

    factory.build = _build_with_reply  # type: ignore[assignment]

    with (
        patch.object(FakeLLMGateway, "probe_vision", return_value=False),
        patch.object(FakeLLMGateway, "probe_tool_calling", return_value=False),
    ):
        result = check_provider_connectivity(_make_provider(), llm_factory=factory)

    assert result.success is True
    assert result.supports_vision is False
    assert result.supports_tool_calling is False
    assert "无" in result.message


def test_failure_on_connectivity() -> None:
    factory = FakeLLMGatewayFactory()

    def _build_with_reply(**kwargs):
        gateway = FakeLLMGateway(model_name=kwargs.get("model_name", "test-model"))
        gateway.replies.append("pong")
        factory.gateways.append(gateway)
        factory.build_calls.append(kwargs)
        return gateway

    factory.build = _build_with_reply  # type: ignore[assignment]

    with (
        patch.object(FakeLLMGateway, "chat_completion", side_effect=ConnectionError("refused")),
        patch.object(FakeLLMGateway, "probe_vision") as probe_vision,
        patch.object(FakeLLMGateway, "probe_tool_calling") as probe_tool,
    ):
        result = check_provider_connectivity(_make_provider(), llm_factory=factory)

    assert result.success is False
    assert "refused" in result.message
    assert result.supports_vision is False
    assert result.supports_tool_calling is False
    probe_vision.assert_not_called()
    probe_tool.assert_not_called()


def test_missing_llm_factory_raises() -> None:
    with pytest.raises(TypeError, match="llm_factory"):
        check_provider_connectivity(_make_provider())
