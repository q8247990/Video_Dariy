from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.application.qa.service import (
    QAProviderInvokeError,
    QAProviderNotConfiguredError,
)
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.event_tag_rel import EventTagRel
from src.models.tag_definition import TagDefinition
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource


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

    def get_daily_summary(self, date_text: str) -> dict:
        try:
            target_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError as e:
            raise MCPInvalidArgumentError("date format must be YYYY-MM-DD") from e

        summary = (
            self.db.query(DailySummary).filter(DailySummary.summary_date == target_date).first()
        )
        if not summary:
            raise MCPNotFoundError("summary not found")

        return {
            "date": str(summary.summary_date),
            "summary_title": summary.summary_title,
            "overall_summary": summary.overall_summary,
            "subject_sections": summary.subject_sections_json or [],
            "attention_items": summary.attention_items_json or [],
            "event_count": summary.event_count,
            "generated_at": summary.generated_at.isoformat(),
        }

    def search_events(
        self,
        *,
        start_time: Optional[str],
        end_time: Optional[str],
        camera: Optional[str],
        keywords: Optional[list[str]],
        tags: Optional[list[str]],
    ) -> dict:
        query = self.db.query(EventRecord, VideoSource).join(
            VideoSource,
            EventRecord.source_id == VideoSource.id,
        )

        try:
            if start_time:
                query = query.filter(
                    EventRecord.event_start_time >= datetime.fromisoformat(start_time)
                )
            if end_time:
                query = query.filter(
                    EventRecord.event_start_time <= datetime.fromisoformat(end_time)
                )
        except ValueError as e:
            raise MCPInvalidArgumentError("invalid start_time or end_time") from e

        if camera:
            query = query.filter(VideoSource.camera_name == camera)

        for keyword in keywords or []:
            keyword_text = keyword.strip()
            if keyword_text:
                query = query.filter(
                    or_(
                        EventRecord.description.ilike(f"%{keyword_text}%"),
                        EventRecord.detail.ilike(f"%{keyword_text}%"),
                    )
                )

        tag_list = tags or []
        if tag_list:
            query = query.join(EventTagRel, EventTagRel.event_id == EventRecord.id).join(
                TagDefinition,
                TagDefinition.id == EventTagRel.tag_id,
            )
            query = query.filter(TagDefinition.tag_name.in_(tag_list)).distinct()

        results = query.order_by(EventRecord.event_start_time.desc()).limit(50).all()
        event_ids = [event.id for event, _ in results]
        tags_map = self._build_tags_map(event_ids)

        events_resp = []
        for event, source in results:
            events_resp.append(
                {
                    "id": event.id,
                    "camera_name": source.camera_name,
                    "event_start_time": event.event_start_time.isoformat(),
                    "event_end_time": event.event_end_time.isoformat()
                    if event.event_end_time
                    else None,
                    "object_type": event.object_type,
                    "action_type": event.action_type,
                    "description": event.description,
                    "detail": event.detail,
                    "tags": tags_map.get(event.id, []),
                }
            )
        return {"events": events_resp}

    def get_event_detail(self, event_id: int) -> dict:
        event = self.db.query(EventRecord).filter(EventRecord.id == event_id).first()
        if event is None:
            raise MCPNotFoundError("event not found")

        session = self.db.query(VideoSession).filter(VideoSession.id == event.session_id).first()
        if session is None:
            raise MCPNotFoundError("session not found")

        return {
            "event": {
                "id": event.id,
                "event_start_time": event.event_start_time.isoformat(),
                "event_end_time": event.event_end_time.isoformat()
                if event.event_end_time
                else None,
                "object_type": event.object_type,
                "action_type": event.action_type,
                "description": event.description,
                "detail": event.detail,
            },
            "session": {
                "id": session.id,
                "session_start_time": session.session_start_time.isoformat(),
                "session_end_time": session.session_end_time.isoformat(),
            },
            "video_reference": {
                "playback_url": self.session_playback_url_builder(session.id),
            },
        }

    def get_video_segments(self, event_id: Optional[int], session_id: Optional[int]) -> dict:
        resolved_session_id = session_id
        if not event_id and not resolved_session_id:
            raise MCPInvalidArgumentError("event_id or session_id is required")

        if event_id and not resolved_session_id:
            event = self.db.query(EventRecord).filter(EventRecord.id == event_id).first()
            if event is None:
                raise MCPNotFoundError("event not found")
            resolved_session_id = event.session_id

        session = self.db.query(VideoSession).filter(VideoSession.id == resolved_session_id).first()
        if session is None:
            raise MCPNotFoundError("session not found")

        rels = (
            self.db.query(VideoSessionFileRel)
            .filter(VideoSessionFileRel.session_id == resolved_session_id)
            .order_by(VideoSessionFileRel.sort_index.asc())
            .all()
        )

        files_data = []
        for rel in rels:
            video_file = self.db.query(VideoFile).filter(VideoFile.id == rel.video_file_id).first()
            if video_file:
                files_data.append(
                    {
                        "file_id": video_file.id,
                        "file_name": video_file.file_name,
                        "stream_url": self.stream_url_builder(video_file.id),
                    }
                )

        return {
            "session_id": resolved_session_id,
            "files": files_data,
        }

    def ask_home_monitor(self, question: str) -> dict:
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

    def _build_tags_map(self, event_ids: list[int]) -> dict[int, list[str]]:
        tags_map: dict[int, list[str]] = {event_id: [] for event_id in event_ids}
        if not event_ids:
            return tags_map

        tag_rows = (
            self.db.query(EventTagRel.event_id, TagDefinition.tag_name)
            .join(TagDefinition, TagDefinition.id == EventTagRel.tag_id)
            .filter(EventTagRel.event_id.in_(event_ids))
            .all()
        )
        for event_id, tag_name in tag_rows:
            tags_map[event_id].append(tag_name)
        return tags_map


__all__ = [
    "MCPInvalidArgumentError",
    "MCPNotFoundError",
    "MCPToolService",
    "QAProviderInvokeError",
    "QAProviderNotConfiguredError",
]
