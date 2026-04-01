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
from src.services.video_analysis.enums import EVENT_TYPE_DEFINITIONS

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 2


# ---------------------------------------------------------------------------
# System Prompt 构建
# ---------------------------------------------------------------------------


def _build_agent_system_prompt(
    now: datetime,
    timezone: str,
    home_context: dict[str, Any],
    data_availability: dict[str, Any],
) -> str:
    """构建 agent 的 system prompt，包含家庭上下文和数据可用范围。"""
    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    member_lines = [f"  - {m.get('name', '?')}（{m.get('role_type', '?')}）" for m in members]
    pet_lines = [f"  - {p.get('name', '?')}（{p.get('role_type', '?')}）" for p in pets]

    subject_names = [m.get("name", "") for m in members if m.get("name")]
    subject_names += [p.get("name", "") for p in pets if p.get("name")]

    event_type_lines = [f"  - {item['type']}: {item['desc']}" for item in EVENT_TYPE_DEFINITIONS]

    return f"""你是家庭安防问答助手。根据用户问题，使用工具查询数据，然后基于查询结果回答。

当前时间: {now.isoformat()}
时区: {timezone}

家庭信息:
  名称: {home_profile.get('home_name', '')}
  关注重点: {'、'.join(home_profile.get('focus_points', [])) or '无'}
  成员:
{chr(10).join(member_lines) if member_lines else '    无'}
  宠物:
{chr(10).join(pet_lines) if pet_lines else '    无'}

已知主体: {', '.join(subject_names) if subject_names else '无'}

系统数据范围:
  最早事件日期: {data_availability.get('earliest_event_date') or '无数据'}
  最晚事件日期: {data_availability.get('latest_event_date') or '无数据'}
  总事件数: {data_availability.get('total_event_count', 0)}

事件类型说明:
{chr(10).join(event_type_lines)}

规则:
1. 先调用工具查询数据，再回答用户问题。不要凭空编造。
2. 时间参数使用 ISO 8601 格式，基于上方"当前时间"计算。
3. subjects 参数只能从"已知主体"中选取。
4. 如果第一次查询结果为空或不够，可以调整参数再查一次。
5. 回答时使用中文，语气自然亲切，像家人之间的对话。
6. 如果确实没有相关数据，如实告知用户。"""


# ---------------------------------------------------------------------------
# Agent 结果
# ---------------------------------------------------------------------------


class QAAgentResult:
    def __init__(
        self,
        answer_text: str,
        tool_calls_log: list[dict[str, Any]],
    ):
        self.answer_text = answer_text
        self.tool_calls_log = tool_calls_log


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
    ) -> QAAgentResult:
        """执行 agentic loop，返回最终回答。"""
        # 1. 获取家庭上下文和数据可用范围
        home_context = build_home_context(self.db)
        query_service = HomeQueryService(self.db)
        availability = query_service.get_data_availability()

        import dataclasses

        availability_dict = dataclasses.asdict(availability)

        # 2. 构建 system prompt
        system_prompt = _build_agent_system_prompt(
            now=now,
            timezone=timezone,
            home_context=home_context,
            data_availability=availability_dict,
        )

        # 3. 初始化 messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]

        tool_calls_log: list[dict[str, Any]] = []

        # 4. Agentic loop（最多 MAX_TOOL_ROUNDS 轮）
        for round_idx in range(MAX_TOOL_ROUNDS):
            enforce_token_quota(self.db, self.provider)

            content, tool_calls = self.gateway.chat_completion_with_tools(
                messages=messages,
                tools=QA_TOOLS,
                temperature=0.2,
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
        )
