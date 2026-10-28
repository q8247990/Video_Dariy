from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMGatewayPort(Protocol):
    supports_tool_calling: bool

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> str | None: ...

    def chat_completion_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> tuple[str | None, list[dict[str, Any]] | None]: ...

    def get_last_usage(self) -> dict[str, int] | None: ...

    def get_last_raw_response_text(self) -> str | None: ...

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
