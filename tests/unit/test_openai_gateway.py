"""Unit tests for src.infrastructure.llm.openai_gateway (extra_body forwarding)."""

from __future__ import annotations

from typing import Any

from src.infrastructure.llm.openai_gateway import OpenAICompatGateway
from src.providers.openai_client import OpenAIClient


class _RecordingClient(OpenAIClient):
    def __init__(self) -> None:  # noqa: D401 - simple test fake
        self.calls: list[dict[str, Any]] = []

    def chat_completion(  # type: ignore[override]
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> str | None:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "extra_body": extra_body,
            }
        )
        return "ok"


def test_gateway_forwards_extra_body_to_client() -> None:
    fake = _RecordingClient()
    gateway = OpenAICompatGateway(fake)  # type: ignore[arg-type]

    extra = {"media_io_kwargs": {"video": {"num_frames": -1}}}
    result = gateway.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        extra_body=extra,
    )

    assert result == "ok"
    assert len(fake.calls) == 1
    assert fake.calls[0]["extra_body"] == extra


def test_gateway_default_extra_body_is_none() -> None:
    fake = _RecordingClient()
    gateway = OpenAICompatGateway(fake)  # type: ignore[arg-type]

    gateway.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert fake.calls[0]["extra_body"] is None
