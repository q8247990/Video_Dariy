"""MCP application use case.

Wraps :class:`src.application.mcp.service.MCPToolService` so that the
MCP layer (``src.mcp.tools.call_tool``) can drive every tool through
the composition-root :class:`~src.application.bootstrap.Container`
instead of constructing :class:`OpenAICompatGatewayFactory` ad-hoc
inside ``MCPToolService``. The factory is bound once on the use case;
``MCPToolService`` itself keeps its previous semantics so existing
callers outside the MCP layer still work.

The use case owns **no** SQLAlchemy session: the caller passes the
request-scoped ``Session`` and is responsible for committing any log
rows the underlying service persists.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from src.application.bootstrap import Container
from src.application.mcp.service import MCPInvalidArgumentError, MCPToolService


class MCPToolUseCase:
    """Drive a single MCP tool call through the application layer.

    The use case holds the container (so the LLM gateway factory is
    resolved through the composition root) and the per-request
    ``Session``. URL builders (``stream_url_builder`` /
    ``session_playback_url_builder``) are still caller-supplied so the
    MCP layer can keep wiring its signing paths and base URLs.
    """

    def __init__(
        self,
        *,
        db: Session,
        container: Container,
        stream_url_builder: Callable[[int], str],
        session_playback_url_builder: Callable[[int], str],
    ) -> None:
        self.db = db
        self._container = container
        self._stream_url_builder = stream_url_builder
        self._session_playback_url_builder = session_playback_url_builder

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        locale: Optional[str] = None,
    ) -> dict[str, Any]:
        service = MCPToolService(
            db=self.db,
            stream_url_builder=self._stream_url_builder,
            session_playback_url_builder=self._session_playback_url_builder,
            llm_factory=self._container.llm_factory,
        )
        return _dispatch_tool(service, tool_name, arguments, locale=locale)


def _dispatch_tool(
    service: MCPToolService,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    locale: Optional[str],
) -> dict[str, Any]:
    """Route ``tool_name`` to the matching ``MCPToolService`` method.

    Mirrors the dispatch table in :func:`src.mcp.tools._execute_tool`
    but lives in the application layer so the use case does not depend
    on the FastAPI/MCP transport at all.
    """

    if tool_name == "get_data_availability":
        return service.get_data_availability()
    if tool_name == "search_events":
        return service.search_events(
            start_time=arguments.get("start_time"),
            end_time=arguments.get("end_time"),
            subjects=arguments.get("subjects"),
            keywords=arguments.get("keywords"),
            event_types=arguments.get("event_types"),
            importance_levels=arguments.get("importance_levels"),
            limit=arguments.get("limit", 20),
        )
    if tool_name == "get_sessions":
        return service.get_sessions(
            start_time=arguments.get("start_time"),
            end_time=arguments.get("end_time"),
            subjects=arguments.get("subjects"),
            limit=arguments.get("limit", 20),
        )
    if tool_name == "get_daily_summary":
        return service.get_daily_summary(
            start_date=_required_str(arguments, "start_date"),
            end_date=arguments.get("end_date"),
        )
    if tool_name == "ask_home_monitor":
        return service.ask_home_monitor(
            _required_str(arguments, "question"),
            locale=locale,
        )
    raise MCPInvalidArgumentError(f"unknown tool: {tool_name}")


def _required_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise MCPInvalidArgumentError(f"{key} is required")
    return value


__all__ = ["MCPToolUseCase"]
