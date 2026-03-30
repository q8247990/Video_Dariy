import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class OpenAIClient:
    """
    Generic client for interacting with OpenAI-compatible endpoints.
    Can be used for both QA and Vision (if the provider supports standard vision messages).
    """

    def __init__(self, api_base_url: str, api_key: str, model_name: str, timeout: int = 60):
        # Clean up api_base_url (some providers expect /v1/chat/completions directly, some just /v1)
        self.api_base_url = api_base_url.rstrip("/")
        if not self.api_base_url.endswith("/v1") and "v1" not in self.api_base_url:
            self.api_base_url = f"{self.api_base_url}/v1"

        self.api_key = api_key
        self.model_name = model_name
        self.timeout = timeout
        self.last_usage: dict[str, int] | None = None
        self.last_raw_response_text: str | None = None

    def _build_default_request_extras(self) -> dict[str, Any]:
        model_name = self.model_name.strip().lower()
        if "qwen" in model_name:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {}

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Optional[str]:
        url = f"{self.api_base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model_name, "messages": messages, "temperature": temperature}
        payload.update(self._build_default_request_extras())
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                self.last_raw_response_text = response.text
                response.raise_for_status()
                data = response.json()
                self._extract_usage(data)
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Error calling OpenAI API: {e}")
            raise e

    def chat_completion_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
        """带 tool_calling 的 chat completion，返回 (content, tool_calls)。"""
        url = f"{self.api_base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "tools": tools,
        }
        payload.update(self._build_default_request_extras())
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                self.last_raw_response_text = response.text
                response.raise_for_status()
                data = response.json()
                self._extract_usage(data)
                message = data["choices"][0]["message"]
                content = message.get("content")
                tool_calls = message.get("tool_calls")
                return content, tool_calls
        except Exception as e:
            logger.error(f"Error calling OpenAI API with tools: {e}")
            raise e

    def probe_tool_calling(self) -> bool:
        """探测模型是否支持 tool_calling。

        发送一个带 dummy tool 的最小请求，检查响应中是否包含 tool_calls。
        """
        dummy_tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "description": "Get the current time",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
        ]
        messages = [
            {"role": "system", "content": "You are a helpful assistant. Use tools when needed."},
            {"role": "user", "content": "What time is it now?"},
        ]
        try:
            _, tool_calls = self.chat_completion_with_tools(
                messages=messages,
                tools=dummy_tools,
                temperature=0,
                max_tokens=64,
            )
            return bool(tool_calls)
        except Exception as e:
            logger.info("probe_tool_calling failed: %s", e)
            return False

    def probe_vision(self) -> bool:
        """探测模型是否支持视觉输入。

        发送一个带 1x1 像素图片的最小请求，检查模型是否正常响应。
        """
        # 1x1 白色 PNG，base64 编码
        tiny_image = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image in one word."},
                    {"type": "image_url", "image_url": {"url": tiny_image}},
                ],
            },
        ]
        try:
            result = self.chat_completion(
                messages=messages,
                temperature=0,
                max_tokens=16,
            )
            return result is not None
        except Exception as e:
            logger.info("probe_vision failed: %s", e)
            return False

    def _extract_usage(self, data: dict[str, Any]) -> None:
        """从响应中提取 usage 信息。"""
        usage = data.get("usage")
        if isinstance(usage, dict):

            def _to_int(value: Any) -> int:
                try:
                    return int(value or 0)
                except (TypeError, ValueError):
                    return 0

            self.last_usage = {
                "prompt_tokens": _to_int(usage.get("prompt_tokens", 0)),
                "completion_tokens": _to_int(usage.get("completion_tokens", 0)),
                "total_tokens": _to_int(usage.get("total_tokens", 0)),
            }
        else:
            self.last_usage = None
