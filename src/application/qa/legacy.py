import dataclasses
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.application.qa.evidence_compressor import compress_evidence
from src.application.qa.planner import (
    build_retrieval_plan,
    normalize_query_plan,
    parse_intent_output,
)
from src.application.qa.retriever import (
    retrieve_daily_summaries,
    retrieve_events,
    retrieve_sessions,
)
from src.application.qa.schemas import EventEvidence, QARequest, QAResult
from src.models.chat_query_log import ChatQueryLog
from src.services.home_profile import build_home_context
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.prompt_builder.v2.qa_answer import build_qa_answer_prompt
from src.services.prompt_builder.v2.qa_intent import build_qa_intent_prompt

logger = logging.getLogger(__name__)


class QAProviderInvokeError(RuntimeError):
    pass


class LegacyQAStrategy:
    """QA strategy for providers that do not support tool calling.

    Drives the legacy retrieve-then-answer flow: intent parse -> retrieval
    plan -> evidence compression -> final answer, then writes a
    ``ChatQueryLog``. Kept separate from the agent strategy so each path
    is independently testable.
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
        home_context = build_home_context(self.db)

        query_plan = self._parse_intent(question, request.now, request.timezone, home_context)

        retrieval_plan = build_retrieval_plan(query_plan)

        daily_summaries = retrieve_daily_summaries(self.db, retrieval_plan)
        sessions = retrieve_sessions(self.db, retrieval_plan, query_plan.question_mode)
        events = retrieve_events(self.db, retrieval_plan, query_plan.question_mode)

        evidence = compress_evidence(
            home_context=home_context,
            query_plan=query_plan,
            daily_summaries=daily_summaries,
            sessions=sessions,
            events=events,
        )

        answer_text = self._generate_answer(
            question,
            request.now,
            request.timezone,
            evidence.home_context_text,
            query_plan,
            evidence,
            request.locale,
        )

        referenced_events = list(events)
        session_ids = sorted({e.session_id for e in events})
        referenced_sessions = [s for s in sessions if s.id in session_ids]
        if not referenced_sessions and sessions:
            referenced_sessions = sessions

        if request.write_query_log:
            self._write_log(question, answer_text, query_plan, referenced_events)

        return QAResult(
            question=question,
            answer_text=answer_text,
            query_plan=query_plan,
            referenced_events=referenced_events,
            referenced_sessions=referenced_sessions,
            referenced_daily_summaries=daily_summaries,
            provider_id=self.provider.id,
        )

    def _parse_intent(
        self,
        question: str,
        now: datetime,
        timezone: str,
        home_context: dict[str, Any],
    ) -> Any:
        system_prompt, user_prompt = build_qa_intent_prompt(
            question=question,
            now=now,
            timezone=timezone,
            home_context=home_context,
        )

        try:
            enforce_token_quota(self.db, self.provider)
            raw_text = self.gateway.chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            record_token_usage(
                self.db,
                provider_id=self.provider.id,
                provider_name_snapshot=self.provider.provider_name,
                scene="qa_intent",
                usage=self.gateway.get_last_usage(),
            )
        except Exception as e:
            logger.warning("Intent parse LLM call failed: %s, using default plan", e)
            return normalize_query_plan(None, now)

        raw_dict = parse_intent_output(raw_text or "")
        return normalize_query_plan(raw_dict, now)

    def _generate_answer(
        self,
        question: str,
        now: datetime,
        timezone: str,
        home_context_text: str,
        query_plan: Any,
        evidence: Any,
        locale: str | None = None,
    ) -> str:
        system_prompt, user_prompt = build_qa_answer_prompt(
            question=question,
            now_iso=now.isoformat(),
            timezone=timezone,
            home_context_text=home_context_text,
            query_plan_text=evidence.query_plan_text,
            daily_summary_text=evidence.daily_summary_text,
            session_text=evidence.session_text,
            event_text=evidence.event_text,
            locale=locale,
        )

        try:
            enforce_token_quota(self.db, self.provider)
            answer_text = self.gateway.chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
            )
            record_token_usage(
                self.db,
                provider_id=self.provider.id,
                provider_name_snapshot=self.provider.provider_name,
                scene="qa_answer",
                usage=self.gateway.get_last_usage(),
            )
            return answer_text or ""
        except Exception as e:
            raise QAProviderInvokeError(f"Answer generation failed: {e}") from e

    def _write_log(
        self,
        question: str,
        answer_text: str,
        query_plan: Any,
        events: list[EventEvidence],
    ) -> None:
        try:
            plan_dict = dataclasses.asdict(query_plan)
            if plan_dict.get("time_range"):
                tr = plan_dict["time_range"]
                if isinstance(tr.get("start"), datetime):
                    tr["start"] = tr["start"].isoformat()
                if isinstance(tr.get("end"), datetime):
                    tr["end"] = tr["end"].isoformat()
        except (TypeError, ValueError):
            logger.debug("Failed to serialize QA retrieval plan", exc_info=True)
            plan_dict = None

        log = ChatQueryLog(
            user_question=question,
            parsed_condition_json=plan_dict,
            answer_text=answer_text,
            referenced_event_ids_json=[e.id for e in events],
            provider_id=self.provider.id,
            provider_name_snapshot=self.provider.provider_name,
        )
        self.db.add(log)
        self.db.commit()
