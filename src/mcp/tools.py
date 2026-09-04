import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.application.bootstrap import Container, bootstrap_production
from src.application.mcp.service import MCPInvalidArgumentError, MCPNotFoundError
from src.application.mcp.use_case_tools import MCPToolUseCase
from src.application.qa.service import (
    QAProviderInvokeError,
    QAProviderNotConfiguredError,
)
from src.core.config import settings
from src.core.i18n.locale_directive import get_mcp_field_description, get_mcp_tool_description
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


# Module-level composition root singleton. Bound once at import time
# exactly like ``src.api.deps._container`` so that every MCP request
# reuses the same LLM gateway factory without going through the
# composition root on each call. Tests that need hermetic isolation
# override ``set_container_for_tests`` (see :func:`set_container_for_tests`).
_container: Container = bootstrap_production()


def get_container() -> Container:
    """Return the module-level composition-root container.

    Tests that wish to inject fakes call :func:`set_container_for_tests`
    from a fixture before any MCP request fires.
    """

    return _container


def set_container_for_tests(container: Container) -> None:
    """Replace the module-level container (test-only escape hatch).

    Production code never calls this — it exists so unit tests can
    substitute a fake-bound :class:`Container` without monkeypatching
    :mod:`src.application.bootstrap` or any concrete adapter.
    """

    global _container
    _container = container


def build_tool_use_case(db: Session) -> MCPToolUseCase:
    return MCPToolUseCase(
        db=db,
        container=get_container(),
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


def _build_tools(locale: str | None = None) -> list[dict[str, Any]]:
    return [
        {
            "name": "get_data_availability",
            "description": get_mcp_tool_description("get_data_availability", locale),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "search_events",
            "description": get_mcp_tool_description("search_events", locale),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": get_mcp_field_description("start_time", locale),
                    },
                    "end_time": {
                        "type": "string",
                        "description": get_mcp_field_description("end_time", locale),
                    },
                    "subjects": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": get_mcp_field_description("subjects", locale),
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": get_mcp_field_description("keywords", locale),
                    },
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": get_mcp_field_description("event_types", locale),
                    },
                    "importance_levels": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["low", "medium", "high"]},
                        "description": get_mcp_field_description("importance_levels", locale),
                    },
                    "limit": {
                        "type": "integer",
                        "description": get_mcp_field_description("limit", locale),
                    },
                },
                "required": ["start_time", "end_time"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_sessions",
            "description": get_mcp_tool_description("get_sessions", locale),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": get_mcp_field_description("start_time", locale),
                    },
                    "end_time": {
                        "type": "string",
                        "description": get_mcp_field_description("end_time", locale),
                    },
                    "subjects": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": get_mcp_field_description("subjects", locale),
                    },
                    "limit": {
                        "type": "integer",
                        "description": get_mcp_field_description("limit", locale),
                    },
                },
                "required": ["start_time", "end_time"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_daily_summary",
            "description": get_mcp_tool_description("get_daily_summary", locale),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": get_mcp_field_description("start_date", locale),
                    },
                    "end_date": {
                        "type": "string",
                        "description": get_mcp_field_description("end_date", locale),
                    },
                },
                "required": ["start_date"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ask_home_monitor",
            "description": get_mcp_tool_description("ask_home_monitor", locale),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": get_mcp_field_description("question", locale),
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    ]


def list_tools(locale: str | None = None) -> dict[str, Any]:
    return {"tools": _build_tools(locale)}


def call_tool(
    db: Session,
    tool_name: str,
    arguments: dict[str, Any],
    source: Optional[str],
    user_agent: Optional[str],
    session_id: Optional[str],
    locale: str | None = None,
) -> dict[str, Any]:
    request_data = {
        "arguments": arguments,
        "_meta": {
            "source": source or "unknown",
            "user_agent": user_agent or "unknown",
            "session_id": session_id or "unknown",
        },
    }
    use_case = build_tool_use_case(db)

    try:
        response = use_case.execute(tool_name, arguments, locale=locale)
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
