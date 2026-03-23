from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401
from src.db.base_class import Base
from src.mcp.tools import call_tool, list_tools
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.event_tag_rel import EventTagRel
from src.models.llm_provider import LLMProvider
from src.models.mcp_call_log import McpCallLog
from src.models.tag_definition import TagDefinition
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def _setup_source_session_event(db: Session) -> tuple[int, int, int]:
    source = VideoSource(
        source_name="source-1",
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
    )
    db.add(event)
    db.commit()
    return source.id, session.id, event.id


def test_list_tools_exposes_all_tools() -> None:
    payload = list_tools()

    assert len(payload["tools"]) == 5
    assert {tool["name"] for tool in payload["tools"]} == {
        "get_daily_summary",
        "search_events",
        "get_event_detail",
        "get_video_segments",
        "ask_home_monitor",
    }


def test_call_tool_get_daily_summary_success_and_log() -> None:
    db = _new_db_session()
    try:
        db.add(
            DailySummary(
                summary_date=date(2026, 2, 26),
                summary_title="2026-02-26 家庭日报",
                overall_summary="昨日门口有人短暂停留。",
                subject_sections_json=[],
                attention_items_json=[],
                event_count=1,
                generated_at=datetime(2026, 2, 27, 8, 0, 0),
            )
        )
        db.commit()

        result = call_tool(
            db=db,
            tool_name="get_daily_summary",
            arguments={"date": "2026-02-26"},
            source="robot-a",
            user_agent="pytest",
            session_id="session-a",
        )

        assert result["isError"] is False
        assert result["structuredContent"]["date"] == "2026-02-26"
        assert result["structuredContent"]["event_count"] == 1
        rows = db.query(McpCallLog).all()
        assert len(rows) == 1
        assert rows[0].tool_name == "get_daily_summary"
        assert rows[0].request_json is not None
        assert rows[0].request_json["_meta"]["session_id"] == "session-a"
    finally:
        db.close()


def test_call_tool_search_and_media_paths() -> None:
    db = _new_db_session()
    try:
        source_id, session_id, event_id = _setup_source_session_event(db)
        tag = TagDefinition(tag_name="门口停留", tag_type="custom", enabled=True)
        db.add(tag)
        db.flush()
        db.add(EventTagRel(event_id=event_id, tag_id=tag.id))
        video_file = VideoFile(
            source_id=source_id,
            file_name="1200_1740553920.mp4",
            file_path="/tmp/1200_1740553920.mp4",
            storage_type="local_file",
            file_format="mp4",
            start_time=datetime(2026, 2, 26, 15, 10, 0),
            end_time=datetime(2026, 2, 26, 15, 11, 0),
            parse_status="parsed",
        )
        db.add(video_file)
        db.flush()
        db.add(
            VideoSessionFileRel(session_id=session_id, video_file_id=video_file.id, sort_index=1)
        )
        db.commit()

        search_result = call_tool(
            db=db,
            tool_name="search_events",
            arguments={
                "start_time": "2026-02-26T00:00:00",
                "end_time": "2026-02-26T23:59:59",
                "camera": "门口摄像头",
                "keywords": ["停留"],
                "tags": ["门口停留"],
            },
            source=None,
            user_agent=None,
            session_id="session-b",
        )
        detail_result = call_tool(
            db=db,
            tool_name="get_event_detail",
            arguments={"event_id": event_id},
            source=None,
            user_agent=None,
            session_id="session-b",
        )
        segments_result = call_tool(
            db=db,
            tool_name="get_video_segments",
            arguments={"event_id": event_id},
            source=None,
            user_agent=None,
            session_id="session-b",
        )

        assert search_result["structuredContent"]["events"][0]["id"] == event_id
        assert "门口停留" in search_result["structuredContent"]["events"][0]["tags"]
        assert detail_result["structuredContent"]["session"]["id"] == session_id
        assert detail_result["structuredContent"]["video_reference"]["playback_url"].endswith(
            f"/media/sessions/{session_id}/playback"
        )
        assert segments_result["structuredContent"]["files"][0]["stream_url"].endswith(
            f"/media/files/{video_file.id}/stream"
        )
    finally:
        db.close()


def test_call_tool_ask_home_monitor_success(monkeypatch) -> None:
    db = _new_db_session()
    try:
        _setup_source_session_event(db)
        provider = LLMProvider(
            provider_name="qa-default",
            provider_type="qa_provider",
            api_base_url="https://example.com/v1",
            api_key="dummy-key",
            model_name="gpt-4o-mini",
            timeout_seconds=30,
            retry_count=1,
            enabled=True,
            supports_qa=True,
            is_default_qa=True,
            supports_vision=False,
            is_default_vision=False,
        )
        db.add(provider)
        db.commit()

        import json

        _call_count = {"n": 0}

        def _mock_chat_completion(
            self,
            messages,
            temperature=0.2,
            max_tokens=None,
            response_format=None,
        ):
            _call_count["n"] += 1
            if _call_count["n"] == 1:
                # 意图识别阶段：返回结构化 JSON
                return json.dumps(
                    {
                        "question_mode": "overview",
                        "time_range": {
                            "start": "2026-02-26T12:00:00",
                            "end": "2026-02-26T18:00:00",
                            "time_label": "yesterday_afternoon",
                        },
                        "subjects": [],
                        "event_types": [],
                        "importance_levels": [],
                        "use_daily_summary_first": False,
                        "use_session_summary_first": True,
                        "need_event_details": True,
                        "limit": 30,
                    },
                    ensure_ascii=False,
                )
            # 回答阶段
            return "昨天下午门口有短暂停留。"

        monkeypatch.setattr(
            "src.providers.openai_client.OpenAIClient.chat_completion",
            _mock_chat_completion,
        )

        result = call_tool(
            db=db,
            tool_name="ask_home_monitor",
            arguments={"question": "昨天下午门口发生了什么？"},
            source="robot-c",
            user_agent="pytest",
            session_id="session-c",
        )

        assert result["isError"] is False
        assert result["structuredContent"]["answer_text"] == "昨天下午门口有短暂停留。"
        assert len(result["structuredContent"]["referenced_events"]) >= 1
        assert len(result["structuredContent"]["referenced_sessions"]) >= 1
    finally:
        db.close()


def test_call_tool_invalid_argument_returns_tool_error() -> None:
    db = _new_db_session()
    try:
        result = call_tool(
            db=db,
            tool_name="get_daily_summary",
            arguments={"date": "bad-date"},
            source=None,
            user_agent=None,
            session_id="session-d",
        )

        assert result["isError"] is True
        assert result["structuredContent"]["error"]["code"] == "INVALID_ARGUMENT"
    finally:
        db.close()
