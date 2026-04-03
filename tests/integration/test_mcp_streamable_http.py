from datetime import date, datetime
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.api.deps as api_deps
import src.db.base  # noqa: F401
import src.db.session as db_session_module
from src.core.config import settings
from src.db.base_class import Base
from src.mcp.server import router as mcp_router
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.event_tag_rel import EventTagRel
from src.models.llm_provider import LLMProvider
from src.models.mcp_call_log import McpCallLog
from src.models.system_config import SystemConfig
from src.models.tag_definition import TagDefinition
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource


@pytest.fixture(autouse=True)
def mcp_token_fixture() -> Generator[None, None, None]:
    original_token = settings.MCP_TOKEN
    settings.MCP_TOKEN = "integration-mcp-token"
    try:
        yield
    finally:
        settings.MCP_TOKEN = original_token


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db_session = local_session()
    try:
        yield db_session
    finally:
        db_session.close()


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    app = FastAPI()
    app.include_router(mcp_router)

    def _override_get_db():
        yield db

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    app.dependency_overrides[db_session_module.get_db] = _override_get_db

    with TestClient(app) as test_client:
        yield test_client


def _seed_event_data(db: Session) -> tuple[int, int, int]:
    source = VideoSource(
        source_name="source-http",
        camera_name="门口摄像头",
        location_name="门口",
        source_type="local_directory",
        enabled=True,
    )
    db.add(source)
    db.flush()

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 2, 26, 15, 10, 0),
        session_end_time=datetime(2026, 2, 26, 15, 20, 0),
        total_duration_seconds=600,
        analysis_status="success",
    )
    db.add(session)
    db.flush()

    event = EventRecord(
        source_id=source.id,
        session_id=session.id,
        event_start_time=datetime(2026, 2, 26, 15, 12, 0),
        event_end_time=datetime(2026, 2, 26, 15, 14, 0),
        object_type="human",
        action_type="stay",
        description="一名人员在门口短暂停留。",
        summary="门口短暂停留",
    )
    db.add(event)
    db.commit()
    return source.id, session.id, event.id


def _initialize(client: TestClient, token: str = "integration-mcp-token") -> str:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
        headers={"X-MCP-Token": token},
    )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "home-monitor-mcp"
    session_id = response.headers.get("Mcp-Session-Id")
    assert isinstance(session_id, str)

    initialized_response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers={"X-MCP-Token": token, "Mcp-Session-Id": session_id},
    )
    assert initialized_response.status_code == 202
    return session_id


def _initialize_with_version(
    client: TestClient,
    protocol_version: str,
    token: str = "integration-mcp-token",
):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
        headers={"X-MCP-Token": token},
    )


def test_mcp_initialize_requires_valid_token(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"X-MCP-Token": "bad-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "invalid mcp token"


def test_mcp_initialize_supports_astrbot_protocol_version(client: TestClient) -> None:
    response = _initialize_with_version(client, "2024-11-05")

    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2024-11-05"
    assert isinstance(response.headers.get("Mcp-Session-Id"), str)


def test_mcp_initialize_negotiates_newer_client_protocol_version(client: TestClient) -> None:
    response = _initialize_with_version(client, "2025-11-25")

    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-06-18"
    assert response.headers.get("MCP-Protocol-Version") == "2025-06-18"


def test_mcp_tools_list_and_get_daily_summary(client: TestClient, db: Session) -> None:
    db.add(
        DailySummary(
            summary_date=date(2026, 2, 26),
            summary_title="2026-02-26 家庭日报",
            overall_summary="昨日门口有人停留",
            subject_sections_json=[],
            attention_items_json=[],
            event_count=1,
            generated_at=datetime(2026, 2, 27, 8, 0, 0),
        )
    )
    db.commit()

    session_id = _initialize(client)
    list_response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={
            "X-MCP-Token": "integration-mcp-token",
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": "2025-06-18",
        },
    )
    call_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_daily_summary", "arguments": {"start_date": "2026-02-26"}},
        },
        headers={
            "X-MCP-Token": "integration-mcp-token",
            "X-MCP-Source": "it-robot",
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": "2025-06-18",
        },
    )

    assert list_response.status_code == 200
    assert len(list_response.json()["result"]["tools"]) == 5
    assert call_response.status_code == 200
    tool_result = call_response.json()["result"]
    assert tool_result["isError"] is False
    assert tool_result["structuredContent"]["summaries"][0]["date"] == "2026-02-26"


def test_mcp_search_and_media_calls(client: TestClient, db: Session) -> None:
    _, _, event_id = _seed_event_data(db)
    tag = TagDefinition(tag_name="门口停留", tag_type="custom", enabled=True)
    db.add(tag)
    db.flush()
    db.add(EventTagRel(event_id=event_id, tag_id=tag.id))
    db.commit()

    session_id = _initialize(client)

    search_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "search_events",
                "arguments": {
                    "start_time": "2026-02-26T00:00:00",
                    "end_time": "2026-02-26T23:59:59",
                    "keywords": ["停留"],
                },
            },
        },
        headers={
            "X-MCP-Token": "integration-mcp-token",
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": "2025-06-18",
        },
    )

    assert search_response.json()["result"]["structuredContent"]["events"][0]["id"] == event_id


def test_mcp_errors_and_logs(client: TestClient, db: Session) -> None:
    db.add(
        DailySummary(
            summary_date=date(2026, 2, 26),
            summary_title="2026-02-26 家庭日报",
            overall_summary="昨日门口有人停留",
            subject_sections_json=[],
            attention_items_json=[],
            event_count=1,
            generated_at=datetime(2026, 2, 27, 8, 0, 0),
        )
    )
    db.commit()
    session_id = _initialize(client)

    invalid_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "get_daily_summary", "arguments": {"start_date": "bad-date"}},
        },
        headers={
            "X-MCP-Token": "integration-mcp-token",
            "X-MCP-Source": "log-checker",
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": "2025-06-18",
            "User-Agent": "pytest-agent",
        },
    )
    no_session_response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}},
        headers={"X-MCP-Token": "integration-mcp-token", "MCP-Protocol-Version": "2025-06-18"},
    )

    assert invalid_response.status_code == 200
    assert invalid_response.json()["result"]["isError"] is True
    assert (
        invalid_response.json()["result"]["structuredContent"]["error"]["code"]
        == "INVALID_ARGUMENT"
    )
    assert no_session_response.status_code == 200
    assert len(no_session_response.json()["result"]["tools"]) == 5

    row = db.query(McpCallLog).filter(McpCallLog.tool_name == "get_daily_summary").one()
    assert row.status == "failed"
    assert row.request_json is not None
    assert row.request_json["_meta"]["source"] == "log-checker"
    assert row.request_json["_meta"]["user_agent"] == "pytest-agent"


def test_mcp_disabled_and_db_token_override(client: TestClient, db: Session) -> None:
    db.add(SystemConfig(config_key="mcp_enabled", config_value=False))
    db.commit()

    disabled_response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 30, "method": "initialize", "params": {}},
        headers={"X-MCP-Token": "integration-mcp-token"},
    )
    assert disabled_response.status_code == 401
    assert disabled_response.json()["error"]["message"] == "mcp service is disabled"

    db.query(SystemConfig).delete()
    db.add(SystemConfig(config_key="mcp_token", config_value="db-token-override"))
    db.commit()

    old_token_response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 31, "method": "initialize", "params": {}},
        headers={"X-MCP-Token": "integration-mcp-token"},
    )
    new_token_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 32,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
        },
        headers={"X-MCP-Token": "db-token-override"},
    )

    assert old_token_response.status_code == 401
    assert new_token_response.status_code == 200


def test_mcp_ask_home_monitor(client: TestClient, db: Session, monkeypatch) -> None:
    _seed_event_data(db)
    db.add(
        DailySummary(
            summary_date=date(2026, 2, 26),
            summary_title="2026-02-26 家庭日报",
            overall_summary="昨日门口有人停留",
            subject_sections_json=[],
            attention_items_json=[],
            event_count=1,
            generated_at=datetime(2026, 2, 27, 8, 0, 0),
        )
    )
    db.add(
        LLMProvider(
            provider_name="qa-http",
            provider_type="qa_provider",
            api_base_url="https://example.com/v1",
            api_key="dummy",
            model_name="gpt-4o-mini",
            timeout_seconds=30,
            retry_count=1,
            enabled=True,
            supports_qa=True,
            is_default_qa=True,
            supports_vision=False,
            is_default_vision=False,
        )
    )
    db.commit()

    def _mock_chat_completion(
        self,
        messages,
        temperature=0.2,
        max_tokens=None,
        response_format=None,
    ):
        return "昨天下午门口有短暂停留。"

    monkeypatch.setattr(
        "src.providers.openai_client.OpenAIClient.chat_completion",
        _mock_chat_completion,
    )

    session_id = _initialize(client)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {
                "name": "ask_home_monitor",
                "arguments": {"question": "昨天下午门口发生了什么？"},
            },
        },
        headers={
            "X-MCP-Token": "integration-mcp-token",
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": "2025-06-18",
        },
    )

    assert response.status_code == 200
    body = response.json()["result"]
    assert body["isError"] is False
    assert body["structuredContent"]["answer_text"] == "昨天下午门口有短暂停留。"


def test_mcp_ask_home_monitor_without_provider(client: TestClient, db: Session) -> None:
    _seed_event_data(db)
    session_id = _initialize(client)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 41,
            "method": "tools/call",
            "params": {
                "name": "ask_home_monitor",
                "arguments": {"question": "昨天下午门口发生了什么？"},
            },
        },
        headers={
            "X-MCP-Token": "integration-mcp-token",
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": "2025-06-18",
        },
    )

    assert response.status_code == 200
    body = response.json()["result"]
    assert body["isError"] is True
    assert body["structuredContent"]["error"]["code"] == "INTERNAL_ERROR"


def test_mcp_delete_session(client: TestClient) -> None:
    session_id = _initialize(client)
    response = client.delete(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
    )

    assert response.status_code == 204

    # After delete, tools/list still works (stateless)
    list_response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={
            "X-MCP-Token": "integration-mcp-token",
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": "2025-06-18",
        },
    )
    assert list_response.status_code == 200
    assert len(list_response.json()["result"]["tools"]) == 5
