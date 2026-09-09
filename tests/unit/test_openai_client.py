import json
from typing import Any

import httpx
import pytest

from src.providers.openai_client import OpenAIClient


class _MockResponse:
    """Stand-in for an ``httpx.Response`` shaped to satisfy the client parser."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.text = json.dumps(payload)
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://example.com/v1/chat/completions"),
                response=httpx.Response(status_code=self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _DummyClient:
    """httpx.Client double: a programmable scripted-response recorder.

    The factory monkey-patches ``httpx.Client`` so the real
    :class:`~src.providers.openai_client.OpenAIClient` consumes this
    fake from the outside; the production code never references its
    private ``_http_client`` attribute.

    ``scripted_responses`` is a queue consumed left-to-right. Each
    entry is either:

    * an ``int`` (interpreted as a ``status_code`` paired with a valid
      success payload — empty string content);
    * an :class:`Exception` instance (raised verbatim);
    * a ``(status_code, payload)`` tuple (full control over the body).
    """

    def __init__(
        self,
        recorder: list[dict[str, Any]],
        scripted_responses: list[Any],
    ) -> None:
        self._recorder = recorder
        self._scripted_responses = list(scripted_responses)

    def post(self, url: str, headers: dict[str, Any], json: dict[str, Any]) -> _MockResponse:
        self._recorder.append({"url": url, "headers": headers, "json": json})
        entry = self._scripted_responses.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, tuple):
            status_code, payload = entry
        else:
            status_code = int(entry)
            payload = {"choices": [{"message": {"content": ""}}]}
        return _MockResponse(payload, status_code=status_code)

    def close(self) -> None:
        return None


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    recorder: list[dict[str, Any]],
    scripted_responses: list[Any] | None = None,
) -> OpenAIClient:
    """Build an :class:`OpenAIClient` against the scripted ``httpx.Client`` stub."""

    def _client_factory(*_args: Any, **_kwargs: Any) -> _DummyClient:
        return _DummyClient(recorder, scripted_responses or [])

    monkeypatch.setattr("src.providers.openai_client.httpx.Client", _client_factory)
    return OpenAIClient(
        api_base_url="http://example.com/v1",
        api_key="dummy",
        model_name="test-model",
    )


def test_qwen_model_adds_disable_thinking_payload(monkeypatch) -> None:
    recorded_requests: list[dict[str, Any]] = []

    def _client_factory(*args: Any, **kwargs: Any) -> _DummyClient:
        del args, kwargs
        return _DummyClient(
            recorded_requests,
            [
                (
                    200,
                    {
                        "choices": [{"message": {"content": "pong"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )
            ],
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

    def _client_factory(*args: Any, **kwargs: Any) -> _DummyClient:
        del args, kwargs
        return _DummyClient(
            recorded_requests, [(200, {"choices": [{"message": {"content": "pong"}}]})]
        )

    monkeypatch.setattr("src.providers.openai_client.httpx.Client", _client_factory)

    client = OpenAIClient(
        api_base_url="http://example.com/v1",
        api_key="dummy",
        model_name="MiniCPM-O",
    )

    response_text = client.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert response_text == "pong"
    assert "chat_template_kwargs" not in recorded_requests[0]["json"]


def test_chat_completion_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    recorded_requests: list[dict[str, Any]] = []
    # First two responses are 429 (retryable); third is success.
    client = _make_client(
        monkeypatch,
        recorded_requests,
        [429, 429, (200, {"choices": [{"message": {"content": "ok"}}]})],
    )

    result = client.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert result == "ok"
    assert len(recorded_requests) == 3


def test_chat_completion_retries_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    recorded_requests: list[dict[str, Any]] = []
    # First two attempts raise TimeoutException (retryable); third succeeds.
    client = _make_client(
        monkeypatch,
        recorded_requests,
        [
            httpx.TimeoutException("timed out"),
            httpx.TimeoutException("timed out"),
            (200, {"choices": [{"message": {"content": "ok"}}]}),
        ],
    )

    result = client.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert result == "ok"
    assert len(recorded_requests) == 3


def test_chat_completion_raises_on_empty_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    recorded_requests: list[dict[str, Any]] = []
    client = _make_client(
        monkeypatch,
        recorded_requests,
        [(200, {"choices": []})],
    )

    with pytest.raises(ValueError, match="no choices"):
        client.chat_completion(messages=[{"role": "user", "content": "hi"}])


def test_chat_completion_raises_on_missing_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    recorded_requests: list[dict[str, Any]] = []
    client = _make_client(monkeypatch, recorded_requests, [(200, {})])

    with pytest.raises(ValueError, match="no choices"):
        client.chat_completion(messages=[{"role": "user", "content": "hi"}])


def test_close_releases_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``close()`` propagates to the underlying ``httpx.Client`` stub."""
    calls: list[None] = []

    class _CloseSpy:
        def post(self, url: str, headers: dict[str, Any], json: dict[str, Any]) -> _MockResponse:
            del url, headers, json
            return _MockResponse({"choices": [{"message": {"content": "ok"}}]})

        def close(self) -> None:
            calls.append(None)

    monkeypatch.setattr(
        "src.providers.openai_client.httpx.Client", lambda *a, **kw: _CloseSpy()
    )

    client = OpenAIClient(
        api_base_url="http://example.com/v1",
        api_key="dummy",
        model_name="test-model",
    )

    client.close()

    assert len(calls) == 1


def test_chat_completion_no_retry_on_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.providers.openai_client.time.sleep", lambda _: None)
    recorded_requests: list[dict[str, Any]] = []
    # 400 is non-retryable — the client must not loop on it.
    client = _make_client(
        monkeypatch,
        recorded_requests,
        [(400, {"error": "bad request"})],
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert len(recorded_requests) == 1


def test_chat_completion_merges_extra_body(monkeypatch) -> None:
    recorded_requests: list[dict[str, Any]] = []

    def _client_factory(*args: Any, **kwargs: Any) -> _DummyClient:
        del args, kwargs
        return _DummyClient(
            recorded_requests,
            [
                (
                    200,
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )
            ],
        )

    monkeypatch.setattr("src.providers.openai_client.httpx.Client", _client_factory)

    client = OpenAIClient(
        api_base_url="http://example.com/v1",
        api_key="dummy",
        model_name="qwen3.5-9b",
    )

    extra = {
        "media_io_kwargs": {
            "video": {
                "fps": 19.955,
                "total_num_frames": 5985,
                "frames_indices": [24, 130, 480, 491, 1210],
                "num_frames": -1,
            }
        }
    }
    client.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        extra_body=extra,
    )

    sent = recorded_requests[0]["json"]
    assert sent["media_io_kwargs"] == extra["media_io_kwargs"]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_completion_default_extra_body_is_no_op(monkeypatch) -> None:
    recorded_requests: list[dict[str, Any]] = []

    def _client_factory(*args: Any, **kwargs: Any) -> _DummyClient:
        del args, kwargs
        return _DummyClient(
            recorded_requests,
            [
                (
                    200,
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )
            ],
        )

    monkeypatch.setattr("src.providers.openai_client.httpx.Client", _client_factory)

    client = OpenAIClient(
        api_base_url="http://example.com/v1",
        api_key="dummy",
        model_name="qwen3.5-9b",
    )

    client.chat_completion(messages=[{"role": "user", "content": "hi"}])

    sent = recorded_requests[0]["json"]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert "media_io_kwargs" not in sent
    assert "extra_body" not in sent
