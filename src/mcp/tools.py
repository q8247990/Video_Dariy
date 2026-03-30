import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.application.mcp.service import MCPInvalidArgumentError, MCPNotFoundError, MCPToolService
from src.application.qa.service import (
    QAProviderInvokeError,
    QAProviderNotConfiguredError,
)
from src.core.config import settings
from src.mcp.auth import log_mcp_call

MCP_ERROR_INVALID_ARGUMENT = "INVALID_ARGUMENT"
MCP_ERROR_NOT_FOUND = "NOT_FOUND"
MCP_ERROR_INTERNAL = "INTERNAL_ERROR"

LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", LATEST_PROTOCOL_VERSION}


def negotiate_protocol_version(requested_version: str) -> str:
    if requested_version in SUPPORTED_PROTOCOL_VERSIONS:
        return requested_version
    return LATEST_PROTOCOL_VERSION


def _build_stream_url(file_id: int) -> str:
    return f"{settings.API_V1_STR}/media/files/{file_id}/stream"


def _build_session_playback_url(session_id: int) -> str:
    return f"{settings.API_V1_STR}/media/sessions/{session_id}/playback"


def build_tool_service(db: Session) -> MCPToolService:
    return MCPToolService(
        db=db,
        stream_url_builder=_build_stream_url,
        session_playback_url_builder=_build_session_playback_url,
    )


def _tool_result(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ],
        "structuredContent": payload,
        "isError": is_error,
    }


def _tool_error(code: str, message: str) -> dict[str, Any]:
    return _tool_result({"error": {"code": code, "message": message}}, is_error=True)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_data_availability",
        "description": "查询系统中数据的时间范围和视频源列表",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "search_events",
        "description": "按时间范围和过滤条件查询事件列表",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "开始时间，ISO 8601 格式",
                },
                "end_time": {
                    "type": "string",
                    "description": "结束时间，ISO 8601 格式",
                },
                "subjects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "主体名称列表",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词列表，匹配事件标题/摘要/详情",
                },
                "event_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "事件类型列表",
                },
                "importance_levels": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["low", "medium", "high"]},
                    "description": "重要程度列表",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量上限，默认 20",
                },
            },
            "required": ["start_time", "end_time"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_sessions",
        "description": "按时间范围和主体查询 session 摘要列表",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "开始时间，ISO 8601 格式",
                },
                "end_time": {
                    "type": "string",
                    "description": "结束时间，ISO 8601 格式",
                },
                "subjects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "主体名称列表",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量上限，默认 20",
                },
            },
            "required": ["start_time", "end_time"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_daily_summary",
        "description": "按日期范围查询日报",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "开始日期，格式 YYYY-MM-DD",
                },
                "end_date": {
                    "type": "string",
                    "description": "结束日期，格式 YYYY-MM-DD",
                },
            },
            "required": ["start_date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ask_home_monitor",
        "description": "对家庭监控数据进行自然语言问答",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "自然语言问题"},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]

TOOL_MAP = {tool["name"]: tool for tool in TOOLS}


def list_tools() -> dict[str, Any]:
    return {"tools": TOOLS}


def _execute_tool(
    service: MCPToolService, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    handlers = {
        "get_data_availability": lambda: service.get_data_availability(),
        "search_events": lambda: service.search_events(
            start_time=_optional_str(arguments.get("start_time")),
            end_time=_optional_str(arguments.get("end_time")),
            subjects=_optional_str_list(arguments.get("subjects")),
            keywords=_optional_str_list(arguments.get("keywords")),
            event_types=_optional_str_list(arguments.get("event_types")),
            importance_levels=_optional_str_list(arguments.get("importance_levels")),
            limit=arguments.get("limit", 20),
        ),
        "get_sessions": lambda: service.get_sessions(
            start_time=_optional_str(arguments.get("start_time")),
            end_time=_optional_str(arguments.get("end_time")),
            subjects=_optional_str_list(arguments.get("subjects")),
            limit=arguments.get("limit", 20),
        ),
        "get_daily_summary": lambda: service.get_daily_summary(
            start_date=_required_str(arguments, "start_date"),
            end_date=_optional_str(arguments.get("end_date")),
        ),
        "ask_home_monitor": lambda: service.ask_home_monitor(_required_str(arguments, "question")),
    }
    handler = handlers.get(tool_name)
    if handler is None:
        raise MCPInvalidArgumentError("tool not found")
    return handler()


def call_tool(
    db: Session,
    tool_name: str,
    arguments: dict[str, Any],
    source: Optional[str],
    user_agent: Optional[str],
    session_id: Optional[str],
) -> dict[str, Any]:
    request_data = {
        "arguments": arguments,
        "_meta": {
            "source": source or "unknown",
            "user_agent": user_agent or "unknown",
            "session_id": session_id or "unknown",
        },
    }
    service = build_tool_service(db)

    try:
        response = _execute_tool(service, tool_name, arguments)
    except MCPInvalidArgumentError as exc:
        error_result = _tool_error(MCP_ERROR_INVALID_ARGUMENT, str(exc))
        log_mcp_call(db, tool_name, request_data, error_result, "failed")
        return error_result
    except MCPNotFoundError as exc:
        error_result = _tool_error(MCP_ERROR_NOT_FOUND, str(exc))
        log_mcp_call(db, tool_name, request_data, error_result, "failed")
        return error_result
    except QAProviderNotConfiguredError:
        error_result = _tool_error(MCP_ERROR_INTERNAL, "no qa provider configured")
        log_mcp_call(db, tool_name, request_data, error_result, "failed")
        return error_result
    except QAProviderInvokeError as exc:
        error_result = _tool_error(MCP_ERROR_INTERNAL, str(exc))
        log_mcp_call(db, tool_name, request_data, error_result, "failed")
        return error_result

    result = _tool_result(response)
    log_mcp_call(db, tool_name, request_data, result, "success")
    return result


def _required_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise MCPInvalidArgumentError(f"{key} is required")
    return value


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MCPInvalidArgumentError("string arguments must be strings")
    return value


def _optional_str_list(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MCPInvalidArgumentError("list arguments must be string arrays")
    return value
