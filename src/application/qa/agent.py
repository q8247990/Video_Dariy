"""QA Agentic Loop。

基于 tool_calling 的问答流程：
1. 查 data_availability 注入 system prompt
2. 最多 2 轮 tool call 循环
3. LLM 基于 tool results 直接生成最终回答
"""

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.application.qa.tools import QA_TOOLS, execute_tool
from src.application.query.service import HomeQueryService
from src.services.home_profile import build_home_context
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.prompt_builder.v2.qa_agent import build_qa_agent_prompt
from src.services.provider_generation_limits import resolve_max_output_tokens

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 2


# ---------------------------------------------------------------------------
# Agent 结果
# ---------------------------------------------------------------------------


def _extract_reference_ids(result_str: str) -> tuple[list[int], list[int]]:
    """从工具结果 JSON 中提取事件/会话引用 ID。

    ``search_events`` 返回 ``{"events": [{"id": ...}]}``，``get_sessions``
    返回 ``{"sessions": [{"id": ...}]}``。解析失败或结构不符时返回空列表，
    引用提取绝不影响主回答流程。
    """
    try:
        payload = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return [], []
    if not isinstance(payload, dict):
        return [], []

    event_ids: list[int] = []
    for item in payload.get("events") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            event_ids.append(item["id"])

    session_ids: list[int] = []
    for item in payload.get("sessions") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            session_ids.append(item["id"])

    return event_ids, session_ids


def _append_unique(target: list[int], values: list[int]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


class QAAgentResult:
    def __init__(
        self,
        answer_text: str,
        tool_calls_log: list[dict[str, Any]],
        referenced_event_ids: list[int] | None = None,
        referenced_session_ids: list[int] | None = None,
    ):
        self.answer_text = answer_text
        self.tool_calls_log = tool_calls_log
        self.referenced_event_ids = referenced_event_ids if referenced_event_ids is not None else []
        self.referenced_session_ids = (
            referenced_session_ids if referenced_session_ids is not None else []
        )


# ---------------------------------------------------------------------------
# Agent 主逻辑
# ---------------------------------------------------------------------------


class QAAgent:
    def __init__(
        self,
        db: Session,
        gateway: Any,
        provider: Any,
    ):
        self.db = db
        self.gateway = gateway
        self.provider = provider

    def run(
        self,
        question: str,
        now: datetime,
        timezone: str,
        locale: str | None = None,
    ) -> QAAgentResult:
        home_context = build_home_context(self.db)
        query_service = HomeQueryService(self.db)
        availability = query_service.get_data_availability()

        import dataclasses

        availability_dict = dataclasses.asdict(availability)

        system_prompt = build_qa_agent_prompt(
            now_iso=now.isoformat(),
            timezone=timezone,
            home_context=home_context,
            data_availability=availability_dict,
            locale=locale,
        )

        # 3. 初始化 messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]

        tool_calls_log: list[dict[str, Any]] = []
        referenced_event_ids: list[int] = []
        referenced_session_ids: list[int] = []

        # 4. Agentic loop（最多 MAX_TOOL_ROUNDS 轮）
        for round_idx in range(MAX_TOOL_ROUNDS):
            enforce_token_quota(self.db, self.provider)

            content, tool_calls = self.gateway.chat_completion_with_tools(
                messages=messages,
                tools=QA_TOOLS,
                temperature=0.2,
                max_tokens=resolve_max_output_tokens(self.provider, scene_default=None),
            )

            record_token_usage(
                self.db,
                provider_id=self.provider.id,
                provider_name_snapshot=self.provider.provider_name,
                scene="qa_agent_loop",
                usage=self.gateway.get_last_usage(),
            )

            # 没有 tool_calls，说明 LLM 已经准备好回答
            if not tool_calls:
                return QAAgentResult(
                    answer_text=content or "",
                    tool_calls_log=tool_calls_log,
                    referenced_event_ids=referenced_event_ids,
                    referenced_session_ids=referenced_session_ids,
                )

            # 将 assistant message（含 tool_calls）追加到 messages
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if content:
                assistant_msg["content"] = content
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            # 执行每个 tool call
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    arguments = {}

                logger.info(
                    "QA Agent round %d: %s(%s)",
                    round_idx + 1,
                    tool_name,
                    arguments,
                )

                result_str = execute_tool(self.db, tool_name, arguments)

                new_event_ids, new_session_ids = _extract_reference_ids(result_str)
                _append_unique(referenced_event_ids, new_event_ids)
                _append_unique(referenced_session_ids, new_session_ids)

                tool_calls_log.append(
                    {
                        "round": round_idx + 1,
                        "tool": tool_name,
                        "arguments": arguments,
                        "result_preview": result_str[:500],
                    }
                )

                # 追加 tool result 到 messages
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    }
                )

        # 5. 轮次用完，做最后一次调用让 LLM 生成回答（不带 tools）
        enforce_token_quota(self.db, self.provider)

        final_answer = self.gateway.chat_completion(
            messages=messages,
            temperature=0.2,
            max_tokens=resolve_max_output_tokens(self.provider, scene_default=None),
        )

        record_token_usage(
            self.db,
            provider_id=self.provider.id,
            provider_name_snapshot=self.provider.provider_name,
            scene="qa_agent_final",
            usage=self.gateway.get_last_usage(),
        )

        return QAAgentResult(
            answer_text=final_answer or "",
            tool_calls_log=tool_calls_log,
            referenced_event_ids=referenced_event_ids,
            referenced_session_ids=referenced_session_ids,
        )
