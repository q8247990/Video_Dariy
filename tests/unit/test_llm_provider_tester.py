from unittest.mock import MagicMock, patch

from src.services.llm_provider_tester import ProviderTestResult, check_provider_connectivity


def _make_provider() -> MagicMock:
    provider = MagicMock()
    provider.api_base_url = "http://localhost:8000/v1"
    provider.api_key = "test-key"
    provider.model_name = "test-model"
    provider.timeout_seconds = 30
    return provider


@patch("src.services.llm_provider_tester.OpenAIClient")
@patch("src.services.llm_provider_tester.OpenAICompatGatewayFactory")
def test_success_with_vision_and_tool_calling(mock_factory_cls, mock_client_cls) -> None:
    mock_gateway = MagicMock()
    mock_gateway.chat_completion.return_value = "pong"
    mock_factory_cls.return_value.build.return_value = mock_gateway

    mock_client = MagicMock()
    mock_client.probe_vision.return_value = True
    mock_client.probe_tool_calling.return_value = True
    mock_client_cls.return_value = mock_client

    provider = _make_provider()
    result = check_provider_connectivity(provider)

    assert isinstance(result, ProviderTestResult)
    assert result.success is True
    assert result.supports_vision is True
    assert result.supports_tool_calling is True
    assert "视觉" in result.message
    assert "工具调用" in result.message


@patch("src.services.llm_provider_tester.OpenAIClient")
@patch("src.services.llm_provider_tester.OpenAICompatGatewayFactory")
def test_success_no_capabilities(mock_factory_cls, mock_client_cls) -> None:
    mock_gateway = MagicMock()
    mock_gateway.chat_completion.return_value = "pong"
    mock_factory_cls.return_value.build.return_value = mock_gateway

    mock_client = MagicMock()
    mock_client.probe_vision.return_value = False
    mock_client.probe_tool_calling.return_value = False
    mock_client_cls.return_value = mock_client

    provider = _make_provider()
    result = check_provider_connectivity(provider)

    assert result.success is True
    assert result.supports_vision is False
    assert result.supports_tool_calling is False
    assert "无" in result.message


@patch("src.services.llm_provider_tester.OpenAIClient")
@patch("src.services.llm_provider_tester.OpenAICompatGatewayFactory")
def test_failure_on_connectivity(mock_factory_cls, mock_client_cls) -> None:
    mock_gateway = MagicMock()
    mock_gateway.chat_completion.side_effect = ConnectionError("refused")
    mock_factory_cls.return_value.build.return_value = mock_gateway

    provider = _make_provider()
    result = check_provider_connectivity(provider)

    assert result.success is False
    assert "refused" in result.message
    assert result.supports_vision is False
    assert result.supports_tool_calling is False
    mock_client_cls.return_value.probe_vision.assert_not_called()
    mock_client_cls.return_value.probe_tool_calling.assert_not_called()
