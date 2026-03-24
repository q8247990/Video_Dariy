from typing import Any

from src.providers.openai_client import OpenAIClient


class _DummyResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _DummyClient:
    def __init__(self, recorder: list[dict[str, Any]], payload: dict[str, Any]) -> None:
        self._recorder = recorder
        self._payload = payload

    def __enter__(self) -> "_DummyClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    def post(self, url: str, headers: dict[str, Any], json: dict[str, Any]) -> _DummyResponse:
        self._recorder.append({"url": url, "headers": headers, "json": json})
        return _DummyResponse(self._payload)


def test_qwen_model_adds_disable_thinking_payload(monkeypatch) -> None:
    recorded_requests: list[dict[str, Any]] = []

    def _client_factory(*args, **kwargs) -> _DummyClient:  # type: ignore[no-untyped-def]
        del args, kwargs
        return _DummyClient(
            recorded_requests,
            {
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr("src.providers.openai_client.httpx.Client", _client_factory)

    client = OpenAIClient(
        api_base_url="http://example.com/v1",
        api_key="dummy",
        model_name="qwen3.5-9b",
    )

    response_text = client.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert response_text == "pong"
    assert recorded_requests[0]["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_non_qwen_model_does_not_add_disable_thinking_payload(monkeypatch) -> None:
    recorded_requests: list[dict[str, Any]] = []

    def _client_factory(*args, **kwargs) -> _DummyClient:  # type: ignore[no-untyped-def]
        del args, kwargs
        return _DummyClient(recorded_requests, {"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr("src.providers.openai_client.httpx.Client", _client_factory)

    client = OpenAIClient(
        api_base_url="http://example.com/v1",
        api_key="dummy",
        model_name="MiniCPM-O",
    )

    response_text = client.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert response_text == "pong"
    assert "chat_template_kwargs" not in recorded_requests[0]["json"]
