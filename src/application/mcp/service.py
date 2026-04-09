"""MCP 工具服务层。

调用共享查询层 HomeQueryService 实现数据查询，
保留 ask_home_monitor 作为自然语言问答入口。
"""

import dataclasses
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from src.application.qa.service import (
    QAProviderInvokeError,
    QAProviderNotConfiguredError,
)
from src.application.query.schemas import (
    DateRange,
    EventFilters,
    TimeRange,
)
from src.application.query.service import HomeQueryService


class MCPNotFoundError(ValueError):
    pass


class MCPInvalidArgumentError(ValueError):
    pass


class MCPToolService:
    def __init__(
        self,
        db: Session,
        stream_url_builder: Callable[[int], str],
        session_playback_url_builder: Callable[[int], str],
    ):
        self.db = db
        self.stream_url_builder = stream_url_builder
        self.session_playback_url_builder = session_playback_url_builder
        self._query_service = HomeQueryService(db)

    # ------------------------------------------------------------------
    # get_data_availability
    # ------------------------------------------------------------------

    def get_data_availability(self) -> dict:
        result = self._query_service.get_data_availability()
        payload = dataclasses.asdict(result)
        if result.earliest_event_date is not None:
            payload["earliest_event_date"] = str(result.earliest_event_date)
        if result.latest_event_date is not None:
            payload["latest_event_date"] = str(result.latest_event_date)
        return payload

    # ------------------------------------------------------------------
    # search_events
    # ------------------------------------------------------------------

    def search_events(
        self,
        *,
        start_time: Optional[str],
        end_time: Optional[str],
        subjects: Optional[list[str]] = None,
        keywords: Optional[list[str]] = None,
        event_types: Optional[list[str]] = None,
        importance_levels: Optional[list[str]] = None,
        limit: int = 20,
    ) -> dict:
        time_range = self._parse_time_range(start_time, end_time)

        filters = EventFilters(
            subjects=subjects or [],
            keywords=keywords or [],
            event_types=event_types or [],
            importance_levels=importance_levels or [],
            limit=limit,
        )

        events = self._query_service.search_events(time_range, filters)
        return {
            "events": [
                {
                    **dataclasses.asdict(e),
                    "event_start_time": e.event_start_time.isoformat(),
                }
                for e in events
            ],
            "total": len(events),
        }

    # ------------------------------------------------------------------
    # get_sessions
    # ------------------------------------------------------------------

    def get_sessions(
        self,
        *,
        start_time: Optional[str],
        end_time: Optional[str],
        subjects: Optional[list[str]] = None,
        limit: int = 20,
    ) -> dict:
        time_range = self._parse_time_range(start_time, end_time)

        sessions = self._query_service.get_sessions(
            time_range=time_range,
            subjects=subjects,
            limit=limit,
        )
        return {
            "sessions": [
                {
                    **dataclasses.asdict(s),
                    "session_start_time": s.session_start_time.isoformat(),
                    "session_end_time": s.session_end_time.isoformat(),
                }
                for s in sessions
            ],
        }

    # ------------------------------------------------------------------
    # get_daily_summary
    # ------------------------------------------------------------------

    def get_daily_summary(
        self,
        start_date: str,
        end_date: Optional[str] = None,
    ) -> dict:
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d").date()
            ed = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else sd
        except ValueError as e:
            raise MCPInvalidArgumentError("date format must be YYYY-MM-DD") from e

        summaries = self._query_service.get_daily_summary(DateRange(start_date=sd, end_date=ed))

        if not summaries:
            raise MCPNotFoundError("summary not found")

        return {
            "summaries": [
                {
                    **dataclasses.asdict(s),
                    "date": str(s.date),
                }
                for s in summaries
            ],
        }

    # ------------------------------------------------------------------
    # ask_home_monitor
    # ------------------------------------------------------------------

    def ask_home_monitor(self, question: str, locale: str | None = None) -> dict:
        clean_question = question.strip()
        if not clean_question:
            raise MCPInvalidArgumentError("question is required")

        from src.application.qa.schemas import QARequest
        from src.application.qa.service import QAService

        service = QAService(self.db)
        result = service.answer(
            QARequest(
                question=clean_question,
                now=datetime.now(),
                timezone="Asia/Shanghai",
                write_query_log=False,
                request_source="mcp",
                locale=locale or "zh-CN",
            )
        )

        return {
            "answer_text": result.answer_text,
            "referenced_events": [
                {"id": item.id, "description": item.summary or item.title}
                for item in result.referenced_events
            ],
            "referenced_sessions": [
                {
                    "id": item.id,
                    "playback_url": self.session_playback_url_builder(item.id),
                }
                for item in result.referenced_sessions
            ],
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_time_range(
        start_time: Optional[str],
        end_time: Optional[str],
    ) -> TimeRange:
        """解析 ISO datetime 字符串为 TimeRange。"""
        if not start_time or not end_time:
            raise MCPInvalidArgumentError("start_time and end_time are required")
        try:
            start = datetime.fromisoformat(start_time)
            end = datetime.fromisoformat(end_time)
        except ValueError as e:
            raise MCPInvalidArgumentError("invalid start_time or end_time") from e
        return TimeRange(start=start, end=end)


__all__ = [
    "MCPInvalidArgumentError",
    "MCPNotFoundError",
    "MCPToolService",
    "QAProviderInvokeError",
    "QAProviderNotConfiguredError",
]
