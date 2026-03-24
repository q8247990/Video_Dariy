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
                response.raise_for_status()
                data = response.json()
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
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Error calling OpenAI API: {e}")
            raise e
