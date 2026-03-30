"""共享查询层服务。

提供 get_data_availability / search_events / get_sessions / get_daily_summary
四个核心查询方法，供 QA Agent 和 MCP 共用。
"""

import logging
from typing import Optional

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from src.application.query.schemas import (
    DailySummaryResult,
    DataAvailability,
    DateRange,
    EventFilters,
    EventResult,
    SessionResult,
    TimeRange,
)
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource

logger = logging.getLogger(__name__)


class HomeQueryService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # get_data_availability
    # ------------------------------------------------------------------

    def get_data_availability(self) -> DataAvailability:
        """返回系统中数据的时间范围和视频源列表。"""
        row = self.db.query(
            func.min(EventRecord.event_start_time),
            func.max(EventRecord.event_start_time),
            func.count(EventRecord.id),
        ).one()

        earliest_dt, latest_dt, total_count = row
        earliest_date = earliest_dt.date() if earliest_dt else None
        latest_date = latest_dt.date() if latest_dt else None

        sources = self.db.query(VideoSource).all()
        source_list = [{"name": s.source_name, "enabled": s.enabled} for s in sources]

        return DataAvailability(
            earliest_event_date=earliest_date,
            latest_event_date=latest_date,
            total_event_count=total_count,
            video_sources=source_list,
        )

    # ------------------------------------------------------------------
    # search_events
    # ------------------------------------------------------------------

    def search_events(
        self,
        time_range: TimeRange,
        filters: Optional[EventFilters] = None,
    ) -> list[EventResult]:
        """按条件查询事件列表。"""
        if filters is None:
            filters = EventFilters()

        query = self.db.query(EventRecord).filter(
            EventRecord.event_start_time >= time_range.start,
            EventRecord.event_start_time <= time_range.end,
        )

        # 事件类型过滤
        if filters.event_types:
            query = query.filter(EventRecord.event_type.in_(filters.event_types))

        # 重要程度过滤
        if filters.importance_levels:
            query = query.filter(EventRecord.importance_level.in_(filters.importance_levels))

        # 主体过滤
        if filters.subjects:
            subject_condition = self._build_subject_filter(filters.subjects)
            if subject_condition is not None:
                query = query.filter(subject_condition)

        # 关键词过滤
        if filters.keywords:
            keyword_conditions = []
            for kw in filters.keywords:
                kw_text = kw.strip()
                if kw_text:
                    keyword_conditions.append(
                        or_(
                            EventRecord.title.ilike(f"%{kw_text}%"),
                            EventRecord.summary.ilike(f"%{kw_text}%"),
                            EventRecord.detail.ilike(f"%{kw_text}%"),
                        )
                    )
            if keyword_conditions:
                query = query.filter(or_(*keyword_conditions))

        query = query.order_by(EventRecord.event_start_time.desc())
        rows = query.limit(filters.limit).all()

        return [self._event_to_result(row) for row in rows]

    # ------------------------------------------------------------------
    # get_sessions
    # ------------------------------------------------------------------

    def get_sessions(
        self,
        time_range: TimeRange,
        subjects: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[SessionResult]:
        """按时间和主体查询 session 摘要列表。"""
        query = self.db.query(VideoSession).filter(
            VideoSession.session_start_time >= time_range.start,
            VideoSession.session_start_time <= time_range.end,
            VideoSession.analysis_status == "success",
        )

        if subjects:
            subject_conditions = []
            for name in subjects:
                subject_conditions.append(
                    text(
                        "EXISTS ("
                        "  SELECT 1 FROM jsonb_array_elements_text("
                        "    main_subjects_json::jsonb"
                        "  ) AS elem"
                        "  WHERE elem = :name"
                        ")"
                    ).bindparams(name=name)
                )
            if subject_conditions:
                query = query.filter(or_(*subject_conditions))

        query = query.order_by(VideoSession.session_start_time.desc())
        rows = query.limit(limit).all()

        return [
            SessionResult(
                id=row.id,
                session_start_time=row.session_start_time,
                session_end_time=row.session_end_time,
                summary_text=row.summary_text or "",
                activity_level=row.activity_level or "",
                main_subjects=row.main_subjects_json or [],
                has_important_event=bool(row.has_important_event),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # get_daily_summary
    # ------------------------------------------------------------------

    def get_daily_summary(
        self,
        date_range: DateRange,
    ) -> list[DailySummaryResult]:
        """按日期范围查询日报。"""
        rows = (
            self.db.query(DailySummary)
            .filter(
                DailySummary.summary_date >= date_range.start_date,
                DailySummary.summary_date <= date_range.end_date,
            )
            .order_by(DailySummary.summary_date.desc())
            .all()
        )

        return [
            DailySummaryResult(
                date=row.summary_date,
                summary_title=row.summary_title or "",
                overall_summary=row.overall_summary or "",
                subject_sections=row.subject_sections_json or [],
                attention_items=row.attention_items_json or [],
                event_count=row.event_count or 0,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _event_to_result(row: EventRecord) -> EventResult:
        """将 EventRecord ORM 对象转换为 EventResult。"""
        subjects: list[str] = []
        for entity in row.related_entities_json or []:
            name = entity.get("matched_profile_name") or entity.get("display_name")
            if name:
                subjects.append(name)

        return EventResult(
            id=row.id,
            event_start_time=row.event_start_time,
            event_type=row.event_type or "",
            importance_level=row.importance_level or "",
            title=row.title or "",
            summary=row.summary or "",
            subjects=subjects,
        )

    @staticmethod
    def _build_subject_filter(subjects: list[str]):
        """构建 PostgreSQL JSON 主体过滤条件。"""
        conditions = []
        for name in subjects:
            conditions.append(
                text(
                    "EXISTS ("
                    "  SELECT 1 FROM jsonb_array_elements("
                    "    related_entities_json::jsonb"
                    "  ) AS elem"
                    "  WHERE ("
                    "    elem->>'matched_profile_name' = :name"
                    "    OR elem->>'display_name' = :name"
                    "  )"
                    "  AND elem->>'recognition_status' IN ('confirmed', 'suspected')"
                    ")"
                ).bindparams(name=name)
            )

        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return or_(*conditions)
