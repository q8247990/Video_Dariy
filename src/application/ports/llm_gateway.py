from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class LLMGatewayPort(Protocol):
    supports_tool_calling: bool

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict[str, Any]] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> Optional[str]: ...

    def chat_completion_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[list[dict[str, Any]]]]: ...

    def get_last_usage(self) -> Optional[dict[str, int]]: ...

    def get_last_raw_response_text(self) -> Optional[str]: ...

    def close(self) -> None: ...

    def probe_vision(self) -> bool: ...

    def probe_tool_calling(self) -> bool: ...


@runtime_checkable
class LLMGatewayFactoryPort(Protocol):
    def build(
        self,
        *,
        api_base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: int,
        supports_tool_calling: bool = False,
    ) -> LLMGatewayPort: ...
