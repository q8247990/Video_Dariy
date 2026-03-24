from typing import Any, Optional

from src.application.ports.llm_gateway import LLMGatewayFactoryPort, LLMGatewayPort
from src.providers.openai_client import OpenAIClient


class OpenAICompatGateway(LLMGatewayPort):
    def __init__(self, client: OpenAIClient):
        self.client = client

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        return self.client.chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    def get_last_usage(self) -> Optional[dict[str, int]]:
        return self.client.last_usage

    def get_last_raw_response_text(self) -> Optional[str]:
        return self.client.last_raw_response_text


class OpenAICompatGatewayFactory(LLMGatewayFactoryPort):
    def build(
        self,
        *,
        api_base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: int,
    ) -> LLMGatewayPort:
        return OpenAICompatGateway(
            OpenAIClient(
                api_base_url=api_base_url,
                api_key=api_key,
                model_name=model_name,
                timeout=timeout_seconds,
            )
        )
