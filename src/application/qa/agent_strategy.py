import logging
from typing import Any

from sqlalchemy.orm import Session

from src.application.qa.agent import QAAgent
from src.application.qa.legacy import LegacyQAStrategy
from src.application.qa.schemas import QARequest, QAResult
from src.models.chat_query_log import ChatQueryLog

logger = logging.getLogger(__name__)


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

        if request.write_query_log:
            self._write_agent_log(question, agent_result.answer_text, agent_result.tool_calls_log)

        return QAResult(
            question=question,
            answer_text=agent_result.answer_text,
            provider_id=self.provider.id,
        )

    def _write_agent_log(
        self,
        question: str,
        answer_text: str,
        tool_calls_log: list[dict[str, Any]],
    ) -> None:
        log = ChatQueryLog(
            user_question=question,
            parsed_condition_json={"mode": "agent", "tool_calls": tool_calls_log},
            answer_text=answer_text,
            referenced_event_ids_json=[],
            provider_id=self.provider.id,
            provider_name_snapshot=self.provider.provider_name,
        )
        self.db.add(log)
        self.db.commit()
