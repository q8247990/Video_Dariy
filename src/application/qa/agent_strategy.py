import logging
from typing import Any

from sqlalchemy.orm import Session

from src.application.qa.agent import QAAgent
from src.application.qa.legacy import LegacyQAStrategy
from src.application.qa.schemas import (
    EventEvidence,
    QARequest,
    QAResult,
    SessionEvidence,
)
from src.models.chat_query_log import ChatQueryLog
from src.models.event_record import EventRecord
from src.models.video_session import VideoSession
from src.services.attention import attention_keys_for_db, is_attention_event

logger = logging.getLogger(__name__)


def _event_to_evidence(
    row: EventRecord, attention_keys: frozenset[str] = frozenset()
) -> EventEvidence:
    return EventEvidence(
        id=row.id,
        session_id=row.session_id,
        event_start_time=row.event_start_time,
        event_type=row.event_type or "",
        attention=is_attention_event(
            event_type=row.event_type,
            related_entities=row.related_entities_json,
            focus_matches=row.focus_matches_json,
            attention_keys=attention_keys,
        ),
        title=row.title or "",
        summary=row.summary or "",
        detail=row.detail or "",
        related_entities=row.related_entities_json or [],
        observed_actions=row.observed_actions_json or [],
        interpreted_state=row.interpreted_state_json or [],
    )


def _session_to_evidence(row: VideoSession) -> SessionEvidence:
    return SessionEvidence(
        id=row.id,
        session_start_time=row.session_start_time,
        session_end_time=row.session_end_time,
        summary_text=row.summary_text or "",
        activity_level=row.activity_level or "",
        main_subjects=row.main_subjects_json or [],
        has_attention_event=bool(row.has_attention_event),
        analysis_notes=row.analysis_notes_json or [],
    )


def _load_referred_events(db: Session, event_ids: list[int]) -> list[EventEvidence]:
    """按引用顺序加载事件证据；数据库中不存在的 ID 被跳过。"""
    if not event_ids:
        return []
    rows = db.query(EventRecord).filter(EventRecord.id.in_(event_ids)).all()
    by_id = {row.id: row for row in rows}
    attention_keys = attention_keys_for_db(db)
    evidence: list[EventEvidence] = []
    for event_id in event_ids:
        row = by_id.get(event_id)
        if row is not None:
            evidence.append(_event_to_evidence(row, attention_keys))
    return evidence


def _load_referred_sessions(db: Session, session_ids: list[int]) -> list[SessionEvidence]:
    """按引用顺序加载会话证据；数据库中不存在的 ID 被跳过。"""
    if not session_ids:
        return []
    rows = db.query(VideoSession).filter(VideoSession.id.in_(session_ids)).all()
    by_id = {row.id: row for row in rows}
    evidence: list[SessionEvidence] = []
    for session_id in session_ids:
        row = by_id.get(session_id)
        if row is not None:
            evidence.append(_session_to_evidence(row))
    return evidence


class AgentQAStrategy:
    """QA strategy for providers that support tool calling.

    Runs the agentic loop (:class:`~src.application.qa.agent.QAAgent`) and
    falls back to :class:`LegacyQAStrategy` if the agent loop raises.
    Kept separate from the legacy strategy so each path is independently
    testable without changing the QA answer contract.
    """

    def __init__(
        self,
        db: Session,
        gateway: Any,
        provider: Any,
    ):
        self.db = db
        self.gateway = gateway
        self.provider = provider

    def execute(self, question: str, request: QARequest) -> QAResult:
        agent = QAAgent(db=self.db, gateway=self.gateway, provider=self.provider)

        try:
            agent_result = agent.run(
                question=question,
                now=request.now,
                timezone=request.timezone,
                locale=request.locale,
            )
        except Exception as e:
            logger.warning("Agent loop failed: %s, falling back to legacy", e)
            return LegacyQAStrategy(self.db, self.gateway, self.provider).execute(question, request)

        referenced_events = _load_referred_events(self.db, agent_result.referenced_event_ids)
        referenced_sessions = _load_referred_sessions(self.db, agent_result.referenced_session_ids)

        if request.write_query_log:
            self._write_agent_log(
                question,
                agent_result.answer_text,
                agent_result.tool_calls_log,
                agent_result.referenced_event_ids,
            )

        return QAResult(
            question=question,
            answer_text=agent_result.answer_text,
            referenced_events=referenced_events,
            referenced_sessions=referenced_sessions,
            provider_id=self.provider.id,
        )

    def _write_agent_log(
        self,
        question: str,
        answer_text: str,
        tool_calls_log: list[dict[str, Any]],
        referenced_event_ids: list[int],
    ) -> None:
        log = ChatQueryLog(
            user_question=question,
            parsed_condition_json={"mode": "agent", "tool_calls": tool_calls_log},
            answer_text=answer_text,
            referenced_event_ids_json=referenced_event_ids,
            provider_id=self.provider.id,
            provider_name_snapshot=self.provider.provider_name,
        )
        self.db.add(log)
        self.db.commit()
