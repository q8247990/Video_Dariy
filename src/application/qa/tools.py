"""QA Agent 工具定义与执行。

定义暴露给 LLM tool_calling 的工具 schema，
以及将 LLM 传入的参数转换为 HomeQueryService 调用的执行函数。
"""

import dataclasses
import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.application.query.schemas import (
    DateRange,
    EventFilters,
    TimeRange,
)
from src.application.query.service import HomeQueryService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool Schema（供 LLM tool_calling 使用）
# ---------------------------------------------------------------------------

TOOL_GET_DATA_AVAILABILITY = {
    "type": "function",
    "function": {
        "name": "get_data_availability",
        "description": "查询系统中数据的时间范围和视频源列表，了解系统里有什么数据",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

TOOL_SEARCH_EVENTS = {
    "type": "function",
    "function": {
        "name": "search_events",
        "description": "按时间范围和过滤条件查询事件列表",
        "parameters": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "开始时间，ISO 8601 格式，例如 2026-03-28T00:00:00",
                },
                "end_time": {
                    "type": "string",
                    "description": "结束时间，ISO 8601 格式，例如 2026-03-28T23:59:59",
                },
                "subjects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "主体名称列表，从已知主体中选取",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词列表，匹配事件标题/摘要/详情",
                },
                "event_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "事件类型列表，从已知事件类型中选取",
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
        },
    },
}

TOOL_GET_SESSIONS = {
    "type": "function",
    "function": {
        "name": "get_sessions",
        "description": "按时间范围和主体查询 session 摘要列表，适合了解某段时间内的活动轨迹",
        "parameters": {
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
        },
    },
}

TOOL_GET_DAILY_SUMMARY = {
    "type": "function",
    "function": {
        "name": "get_daily_summary",
        "description": "按日期范围查询日报，适合了解某天或某段时间的整体概况",
        "parameters": {
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
            "required": ["start_date", "end_date"],
        },
    },
}

QA_TOOLS = [
    TOOL_GET_DATA_AVAILABILITY,
    TOOL_SEARCH_EVENTS,
    TOOL_GET_SESSIONS,
    TOOL_GET_DAILY_SUMMARY,
]


# ---------------------------------------------------------------------------
# 工具执行
# ---------------------------------------------------------------------------


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    """解析 ISO datetime 字符串。"""
    text = value.strip()
    if not text:
        return None
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    return None


def execute_tool(
    db: Session,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """执行工具调用，返回 JSON 字符串结果。"""
    service = HomeQueryService(db)

    try:
        if tool_name == "get_data_availability":
            result = _exec_get_data_availability(service)
        elif tool_name == "search_events":
            result = _exec_search_events(service, arguments)
        elif tool_name == "get_sessions":
            result = _exec_get_sessions(service, arguments)
        elif tool_name == "get_daily_summary":
            result = _exec_get_daily_summary(service, arguments)
        else:
            result = {"error": f"unknown tool: {tool_name}"}
    except Exception as e:
        logger.warning("Tool execution failed: %s(%s) -> %s", tool_name, arguments, e)
        result = {"error": str(e)}

    return json.dumps(result, ensure_ascii=False, default=str)


def _exec_get_data_availability(service: HomeQueryService) -> dict[str, Any]:
    data = service.get_data_availability()
    return dataclasses.asdict(data)


def _exec_search_events(service: HomeQueryService, args: dict[str, Any]) -> dict[str, Any]:
    start = _parse_iso_datetime(args.get("start_time", ""))
    end = _parse_iso_datetime(args.get("end_time", ""))
    if not start or not end:
        return {"error": "start_time and end_time are required (ISO 8601)"}

    filters = EventFilters(
        subjects=args.get("subjects") or [],
        keywords=args.get("keywords") or [],
        event_types=args.get("event_types") or [],
        importance_levels=args.get("importance_levels") or [],
        limit=args.get("limit", 20),
    )

    events = service.search_events(TimeRange(start=start, end=end), filters)
    return {
        "events": [dataclasses.asdict(e) for e in events],
        "total": len(events),
    }


def _exec_get_sessions(service: HomeQueryService, args: dict[str, Any]) -> dict[str, Any]:
    start = _parse_iso_datetime(args.get("start_time", ""))
    end = _parse_iso_datetime(args.get("end_time", ""))
    if not start or not end:
        return {"error": "start_time and end_time are required (ISO 8601)"}

    sessions = service.get_sessions(
        time_range=TimeRange(start=start, end=end),
        subjects=args.get("subjects"),
        limit=args.get("limit", 20),
    )
    return {"sessions": [dataclasses.asdict(s) for s in sessions]}


def _exec_get_daily_summary(service: HomeQueryService, args: dict[str, Any]) -> dict[str, Any]:
    try:
        start_date = datetime.strptime(args["start_date"], "%Y-%m-%d").date()
        end_date = datetime.strptime(args["end_date"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return {"error": "start_date and end_date are required (YYYY-MM-DD)"}

    summaries = service.get_daily_summary(DateRange(start_date=start_date, end_date=end_date))
    return {"summaries": [dataclasses.asdict(s) for s in summaries]}
