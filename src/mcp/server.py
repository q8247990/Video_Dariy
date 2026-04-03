import secrets
from typing import Any, Optional

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from src.api.deps import DB
from src.mcp.auth import authorize
from src.mcp.tools import (
    SUPPORTED_PROTOCOL_VERSIONS,
    call_tool,
    list_tools,
    negotiate_protocol_version,
)

router = APIRouter()

JSONRPC_VERSION = "2.0"


def _jsonrpc_result(result: dict[str, Any], request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _jsonrpc_error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _validate_protocol_header(protocol_version: Optional[str]) -> Optional[JSONResponse]:
    if protocol_version is None:
        return None
    if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_PROTOCOL_VERSION",
                    "message": "unsupported MCP-Protocol-Version",
                }
            },
        )
    return None


async def _parse_payload(
    request: Request,
) -> tuple[Optional[dict[str, Any]], Optional[JSONResponse]]:
    try:
        payload = await request.json()
    except Exception:
        return None, JSONResponse(status_code=400, content=_jsonrpc_error(-32700, "parse error"))
    if not isinstance(payload, dict):
        return None, JSONResponse(
            status_code=400, content=_jsonrpc_error(-32600, "invalid request")
        )
    return payload, None


def _extract_request_fields(
    payload: dict[str, Any],
) -> tuple[Any, str, dict[str, Any], Optional[JSONResponse]]:
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if payload.get("jsonrpc") != JSONRPC_VERSION or not isinstance(method, str):
        return (
            request_id,
            "",
            {},
            JSONResponse(
                status_code=400,
                content=_jsonrpc_error(-32600, "invalid request", request_id),
            ),
        )
    if not isinstance(params, dict):
        return (
            request_id,
            method,
            {},
            JSONResponse(
                status_code=400,
                content=_jsonrpc_error(-32602, "invalid params", request_id),
            ),
        )
    return request_id, method, params, None


def _handle_initialize(request_id: Any, params: dict[str, Any]) -> JSONResponse:
    client_version = params.get("protocolVersion")
    if not isinstance(client_version, str):
        return JSONResponse(
            status_code=400,
            content=_jsonrpc_error(-32602, "unsupported protocolVersion", request_id),
        )
    negotiated_version = negotiate_protocol_version(client_version)
    session_id = secrets.token_urlsafe(24)
    return JSONResponse(
        content=_jsonrpc_result(
            {
                "protocolVersion": negotiated_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "home-monitor-mcp", "version": "0.1.0"},
            },
            request_id,
        ),
        headers={
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": negotiated_version,
        },
    )


def _handle_tools_call(
    db: DB,
    request: Request,
    request_id: Any,
    params: dict[str, Any],
    source: Optional[str],
    session_id: Optional[str],
) -> JSONResponse:
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(tool_name, str):
        return JSONResponse(
            status_code=400,
            content=_jsonrpc_error(-32602, "tool name is required", request_id),
        )
    if not isinstance(arguments, dict):
        return JSONResponse(
            status_code=400,
            content=_jsonrpc_error(-32602, "tool arguments must be an object", request_id),
        )
    result = call_tool(
        db=db,
        tool_name=tool_name,
        arguments=arguments,
        source=source,
        user_agent=request.headers.get("User-Agent"),
        session_id=session_id,
    )
    return JSONResponse(content=_jsonrpc_result(result, request_id))


def _handle_delete() -> Response:
    return Response(status_code=204)


def _dispatch_method(
    db: DB,
    request: Request,
    request_id: Any,
    method: str,
    params: dict[str, Any],
    source: Optional[str],
    session_id: Optional[str],
) -> Response:
    if method == "initialize":
        return _handle_initialize(request_id, params)
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return JSONResponse(content=_jsonrpc_result(list_tools(), request_id))
    if method == "tools/call":
        return _handle_tools_call(db, request, request_id, params, source, session_id)
    if method == "ping":
        return JSONResponse(content=_jsonrpc_result({}, request_id))
    return JSONResponse(
        status_code=404, content=_jsonrpc_error(-32601, "method not found", request_id)
    )


async def _handle_post(
    request: Request,
    db: DB,
    token: Optional[str],
    source: Optional[str],
    session_id: Optional[str],
    protocol_version: Optional[str],
) -> Response:
    protocol_error = _validate_protocol_header(protocol_version)
    if protocol_error is not None:
        return protocol_error

    auth_error = authorize(db, token)
    if auth_error is not None:
        return JSONResponse(status_code=401, content=_jsonrpc_error(-32001, auth_error))

    payload, parse_error = await _parse_payload(request)
    if parse_error is not None:
        return parse_error

    assert payload is not None
    request_id, method, params, request_error = _extract_request_fields(payload)
    if request_error is not None:
        return request_error

    return _dispatch_method(db, request, request_id, method, params, source, session_id)


@router.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def mcp_endpoint(
    request: Request,
    db: DB,
    x_mcp_token: Optional[str] = Header(default=None, alias="X-MCP-Token"),
    x_mcp_source: Optional[str] = Header(default=None, alias="X-MCP-Source"),
    mcp_session_id: Optional[str] = Header(default=None, alias="Mcp-Session-Id"),
    mcp_protocol_version: Optional[str] = Header(default=None, alias="MCP-Protocol-Version"),
) -> Response:
    if request.method == "GET":
        return Response(status_code=200, content="", media_type="text/event-stream")
    if request.method == "DELETE":
        return _handle_delete()
    return await _handle_post(
        request=request,
        db=db,
        token=x_mcp_token,
        source=x_mcp_source,
        session_id=mcp_session_id,
        protocol_version=mcp_protocol_version,
    )
