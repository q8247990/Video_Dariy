import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from src.providers.openai_client import OpenAIClient


class _DummyResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.text = json.dumps(payload)
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _DummyClient:
    def __init__(self, recorder: list[dict[str, Any]], payload: dict[str, Any]) -> None:
        self._recorder = recorder
        self._payload = payload

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
    assert client.last_raw_response_text is not None
    assert '"content": "pong"' in client.last_raw_response_text
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


def _make_client(monkeypatch) -> OpenAIClient:
    monkeypatch.setattr(
        "src.providers.openai_client.httpx.Client",
        lambda *a, **kw: MagicMock(spec=httpx.Client),
    )
    return OpenAIClient(
        api_base_url="http://example.com/v1",
        api_key="dummy",
        model_name="test-model",
    )


def test_chat_completion_retries_on_429(monkeypatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    client = _make_client(monkeypatch)

    call_count = 0
    ok_payload = {"choices": [{"message": {"content": "ok"}}]}

    def _mock_post(url, headers, json):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return _DummyResponse(ok_payload, status_code=429)
        return _DummyResponse(ok_payload, status_code=200)

    client._http_client.post = _mock_post

    result = client.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert result == "ok"
    assert call_count == 3


def test_chat_completion_retries_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    client = _make_client(monkeypatch)

    call_count = 0
    ok_payload = {"choices": [{"message": {"content": "ok"}}]}

    def _mock_post(url, headers, json):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise httpx.TimeoutException("timed out")
        return _DummyResponse(ok_payload, status_code=200)

    client._http_client.post = _mock_post

    result = client.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert result == "ok"
    assert call_count == 3


def test_chat_completion_raises_on_empty_choices(monkeypatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    client = _make_client(monkeypatch)

    client._http_client.post = lambda url, headers, json: _DummyResponse(
        {"choices": []}, status_code=200
    )

    with pytest.raises(ValueError, match="no choices"):
        client.chat_completion(messages=[{"role": "user", "content": "hi"}])


def test_chat_completion_raises_on_missing_choices(monkeypatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    client = _make_client(monkeypatch)

    client._http_client.post = lambda url, headers, json: _DummyResponse({}, status_code=200)

    with pytest.raises(ValueError, match="no choices"):
        client.chat_completion(messages=[{"role": "user", "content": "hi"}])


def test_client_reuses_http_client(monkeypatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    client = _make_client(monkeypatch)

    ok_payload = {"choices": [{"message": {"content": "ok"}}]}
    client._http_client.post = lambda url, headers, json: _DummyResponse(
        ok_payload, status_code=200
    )

    http_client_before = client._http_client
    client.chat_completion(messages=[{"role": "user", "content": "1"}])
    client.chat_completion(messages=[{"role": "user", "content": "2"}])
    assert client._http_client is http_client_before


def test_chat_completion_no_retry_on_400(monkeypatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    client = _make_client(monkeypatch)

    call_count = 0

    def _mock_post(url, headers, json):
        nonlocal call_count
        call_count += 1
        return _DummyResponse({"error": "bad request"}, status_code=400)

    client._http_client.post = _mock_post

    with pytest.raises(httpx.HTTPStatusError):
        client.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert call_count == 1
