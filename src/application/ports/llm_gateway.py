from typing import Any, Optional, Protocol


class LLMGatewayPort(Protocol):
    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict[str, Any]] = None,
    ) -> Optional[str]: ...

    def get_last_usage(self) -> Optional[dict[str, int]]: ...

    def get_last_raw_response_text(self) -> Optional[str]: ...


class LLMGatewayFactoryPort(Protocol):
    def build(
        self,
        *,
        api_base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: int,
    ) -> LLMGatewayPort: ...
