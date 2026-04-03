import logging
from dataclasses import dataclass

from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
from src.models.llm_provider import LLMProvider
from src.providers.openai_client import OpenAIClient

logger = logging.getLogger(__name__)


@dataclass
class ProviderTestResult:
    success: bool
    message: str
    supports_vision: bool
    supports_tool_calling: bool


def check_provider_connectivity(provider: LLMProvider) -> ProviderTestResult:
    """Test provider connectivity and probe capabilities.

    Does NOT modify the provider model — caller is responsible for
    writing results back and committing.
    """
    test_status = "failed"
    test_message = ""
    vision_result = False
    tool_calling_result = False

    client = OpenAIClient(
        api_base_url=provider.api_base_url,
        api_key=provider.api_key,
        model_name=provider.model_name,
        timeout=provider.timeout_seconds,
    )

    try:
        gateway = OpenAICompatGatewayFactory().build(
            api_base_url=provider.api_base_url,
            api_key=provider.api_key,
            model_name=provider.model_name,
            timeout_seconds=provider.timeout_seconds,
        )
        _ = gateway.chat_completion(
            messages=[
                {"role": "system", "content": "You are a connectivity test assistant."},
                {"role": "user", "content": "Reply with 'pong'."},
            ],
            temperature=0,
        )
        test_status = "success"
        test_message = "provider reachable"
    except Exception as e:
        test_message = str(e)[:512]

    if test_status == "success":
        vision_result = client.probe_vision()
        tool_calling_result = client.probe_tool_calling()

        capabilities = []
        if vision_result:
            capabilities.append("视觉")
        if tool_calling_result:
            capabilities.append("工具调用")
        cap_text = "、".join(capabilities) if capabilities else "无"
        test_message = f"连通性正常，检测到能力：{cap_text}"

    return ProviderTestResult(
        success=(test_status == "success"),
        message=test_message,
        supports_vision=vision_result,
        supports_tool_calling=tool_calling_result,
    )
