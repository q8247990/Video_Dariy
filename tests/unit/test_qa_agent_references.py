"""Agent QA 策略引用链路测试。

覆盖 bug 修复：AgentQAStrategy 必须把工具调用检索到的事件/会话透出到
QAResult.referenced_events / referenced_sessions，并在 ChatQueryLog 中
记录真实的事件引用 ID（此前恒为空列表）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.application.qa.agent_strategy import AgentQAStrategy
from src.application.qa.schemas import QARequest
from src.models.chat_query_log import ChatQueryLog
from src.models.event_record import EventRecord
from src.models.llm_provider import LLMProvider
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource

# ---------------------------------------------------------------------------
# AgentQAStrategy 引用透出集成测试（PostgreSQL + fake gateway）
# ---------------------------------------------------------------------------


def _tool_call(call_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "function": {
            "name": tool_name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


_TIME_ARGS = {
    "start_time": "2026-03-15T00:00:00",
    "end_time": "2026-03-15T23:59:59",
}


class FakeToolGateway:
    """第一轮并行调用 search_events + get_sessions，第二轮返回最终回答。

    工具结果由真实的 ``execute_tool`` 对 PostgreSQL 数据产生（全链路验证）。
    """

    def __init__(self) -> None:
        self.with_tools_calls = 0

    def chat_completion_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
    ) -> tuple[str, list[dict[str, Any]]]:
        self.with_tools_calls += 1
        if self.with_tools_calls == 1:
            return "", [
                _tool_call("call_1", "search_events", {**_TIME_ARGS, "limit": 20}),
                _tool_call("call_2", "get_sessions", dict(_TIME_ARGS)),
            ]
        return "测试回答", []

    def chat_completion(self, messages: list[dict[str, Any]], temperature: float) -> str:
        return "轮次用尽后的回答"

    def get_last_usage(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeProvider:
    provider_name = "fake-qa"
    api_base_url = "http://localhost/v1"
    api_key = ""
    model_name = "fake"
    timeout_seconds = 5
    retry_count = 0
    enabled = True
    supports_tool_calling = True
    extra_config_json = {}


def _fake_provider(provider_id: int) -> FakeProvider:
    """``chat_query_log.provider_id`` 是 ``llm_provider`` 外键，id 必须指向真实行。"""
    provider = FakeProvider()
    provider.id = provider_id
    return provider


@pytest.fixture()
def db_session(pg_db: Session) -> Session:
    """Single session against the project-wide ``pg_db`` fixture (PostgreSQL)."""
    return pg_db


def _seed_data(db: Session) -> tuple[int, int, int]:
    """建 source/provider/session/event，返回 (event_id, session_id, provider_id)。"""
    now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    source = VideoSource(
        source_name="test-source",
        camera_name="cam",
        location_name="living-room",
        source_type="local_directory",
    )
    db.add(source)
    db.flush()
    provider = LLMProvider(
        provider_name="fake-qa",
        api_base_url="http://localhost/v1",
        api_key="",
        model_name="fake",
        supports_tool_calling=True,
    )
    db.add(provider)
    db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=now,
        session_end_time=now + timedelta(minutes=5),
        analysis_status="success",
        summary_text="测试会话",
        activity_level="low",
        main_subjects_json=["妈妈"],
        has_important_event=False,
    )
    db.add(session)
    db.flush()
    event = EventRecord(
        source_id=source.id,
        session_id=session.id,
        event_start_time=now + timedelta(minutes=1),
        description="妈妈进入客厅",
        title="妈妈进入客厅",
        summary="妈妈进入客厅",
        detail="详细",
        importance_level="medium",
    )
    db.add(event)
    db.commit()
    return event.id, session.id, provider.id


def test_agent_strategy_exposes_referenced_events_and_sessions(db_session: Session) -> None:
    event_id, session_id, provider_id = _seed_data(db_session)
    gateway = FakeToolGateway()

    strategy = AgentQAStrategy(db=db_session, gateway=gateway, provider=_fake_provider(provider_id))
    result = strategy.execute(
        "上午10点发生了什么",
        QARequest(
            question="上午10点发生了什么",
            now=datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
            timezone="Asia/Shanghai",
            write_query_log=True,
            request_source="web",
            locale="zh-CN",
        ),
    )

    assert result.answer_text == "测试回答"
    # 引用链路透出：事件与会话都来自工具调用检索结果
    assert [e.id for e in result.referenced_events] == [event_id]
    assert result.referenced_events[0].title == "妈妈进入客厅"
    assert [s.id for s in result.referenced_sessions] == [session_id]
    assert result.referenced_sessions[0].summary_text == "测试会话"

    # 问答审计日志记录真实引用 ID
    log = db_session.query(ChatQueryLog).one()
    assert log.referenced_event_ids_json == [event_id]
    assert log.parsed_condition_json["mode"] == "agent"
    assert [call["tool"] for call in log.parsed_condition_json["tool_calls"]] == [
        "search_events",
        "get_sessions",
    ]


def test_agent_strategy_no_tool_calls_yields_empty_references(db_session: Session) -> None:
    class NoToolGateway(FakeToolGateway):
        def chat_completion_with_tools(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            temperature: float,
        ) -> tuple[str, list[dict[str, Any]]]:
            return "无工具回答", []

    _, _, provider_id = _seed_data(db_session)
    gateway = NoToolGateway()

    strategy = AgentQAStrategy(db=db_session, gateway=gateway, provider=_fake_provider(provider_id))
    result = strategy.execute(
        "随便问问",
        QARequest(
            question="随便问问",
            now=datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
            timezone="Asia/Shanghai",
            write_query_log=True,
            request_source="web",
            locale="zh-CN",
        ),
    )

    assert result.answer_text == "无工具回答"
    assert result.referenced_events == []
    assert result.referenced_sessions == []
    log = db_session.query(ChatQueryLog).one()
    assert log.referenced_event_ids_json == []
