import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.application.qa.agent import QAAgent
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
from src.application.qa.schemas import (
    EventEvidence,
    QARequest,
    QAResult,
)
from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
from src.models.chat_query_log import ChatQueryLog
from src.services.home_profile import build_home_context
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.prompt_builder.v2.qa_answer import build_qa_answer_prompt
from src.services.prompt_builder.v2.qa_intent import build_qa_intent_prompt
from src.services.provider_selector import PROVIDER_TYPE_QA, find_enabled_provider

logger = logging.getLogger(__name__)


class QAProviderNotConfiguredError(ValueError):
    pass


class QAProviderInvokeError(RuntimeError):
    pass


class QAService:
    def __init__(self, db: Session):
        self.db = db
        self._gateway_factory = OpenAICompatGatewayFactory()

    def answer(self, request: QARequest) -> QAResult:
        question = request.question.strip()
        if not question:
            raise ValueError("question is required")

        # 1. 获取 provider
        provider = find_enabled_provider(self.db, PROVIDER_TYPE_QA)
        if provider is None:
            raise QAProviderNotConfiguredError("No QA provider configured")
        enforce_token_quota(self.db, provider)

        gateway = self._gateway_factory.build(
            api_base_url=provider.api_base_url,
            api_key=provider.api_key,
            model_name=provider.model_name,
            timeout_seconds=provider.timeout_seconds,
            supports_tool_calling=provider.supports_tool_calling,
        )

        # 2. 根据 provider 能力选择链路
        if gateway.supports_tool_calling:
            return self._answer_via_agent(gateway, provider, question, request)

        return self._answer_via_legacy(gateway, provider, question, request)

    # ------------------------------------------------------------------
    # Agent 链路（supports_tool_calling = True）
    # ------------------------------------------------------------------

    def _answer_via_agent(
        self,
        gateway: Any,
        provider: Any,
        question: str,
        request: QARequest,
    ) -> QAResult:
        agent = QAAgent(db=self.db, gateway=gateway, provider=provider)

        try:
            agent_result = agent.run(
                question=question,
                now=request.now,
                timezone=request.timezone,
            )
        except Exception as e:
            logger.warning("Agent loop failed: %s, falling back to legacy", e)
            return self._answer_via_legacy(gateway, provider, question, request)

        # 日志
        if request.write_query_log:
            self._write_agent_log(
                question=question,
                answer_text=agent_result.answer_text,
                tool_calls_log=agent_result.tool_calls_log,
                provider_id=provider.id,
                provider_name_snapshot=provider.provider_name,
            )

        return QAResult(
            question=question,
            answer_text=agent_result.answer_text,
            provider_id=provider.id,
        )

    # ------------------------------------------------------------------
    # Legacy 链路（supports_tool_calling = False）
    # ------------------------------------------------------------------

    def _answer_via_legacy(
        self,
        gateway: Any,
        provider: Any,
        question: str,
        request: QARequest,
    ) -> QAResult:
        home_context = build_home_context(self.db)

        query_plan = self._parse_intent(
            gateway, provider, question, request.now, request.timezone, home_context
        )

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
            gateway,
            provider,
            question,
            request.now,
            request.timezone,
            evidence.home_context_text,
            query_plan,
            evidence,
        )

        referenced_events = list(events)
        session_ids = sorted({e.session_id for e in events})
        referenced_sessions = [s for s in sessions if s.id in session_ids]
        if not referenced_sessions and sessions:
            referenced_sessions = sessions

        if request.write_query_log:
            self._write_log(
                question,
                answer_text,
                query_plan,
                referenced_events,
                provider.id,
                provider.provider_name,
            )

        return QAResult(
            question=question,
            answer_text=answer_text,
            query_plan=query_plan,
            referenced_events=referenced_events,
            referenced_sessions=referenced_sessions,
            referenced_daily_summaries=daily_summaries,
            provider_id=provider.id,
        )

    # ------------------------------------------------------------------
    # 意图识别
    # ------------------------------------------------------------------

    def _parse_intent(
        self,
        gateway: Any,
        provider: Any,
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
            enforce_token_quota(self.db, provider)
            raw_text = gateway.chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            record_token_usage(
                self.db,
                provider_id=provider.id,
                provider_name_snapshot=provider.provider_name,
                scene="qa_intent",
                usage=gateway.get_last_usage(),
            )
        except Exception as e:
            logger.warning("Intent parse LLM call failed: %s, using default plan", e)
            return normalize_query_plan(None, now)

        raw_dict = parse_intent_output(raw_text or "")
        return normalize_query_plan(raw_dict, now)

    # ------------------------------------------------------------------
    # 最终回答
    # ------------------------------------------------------------------

    def _generate_answer(
        self,
        gateway: Any,
        provider: Any,
        question: str,
        now: datetime,
        timezone: str,
        home_context_text: str,
        query_plan: Any,
        evidence: Any,
    ) -> str:
        system_prompt, user_prompt = build_qa_answer_prompt(
            question=question,
            now_iso=now.isoformat(),
            timezone=timezone,
            home_context_text=home_context_text,
            evidence=evidence,
        )

        try:
            enforce_token_quota(self.db, provider)
            answer_text = gateway.chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
            )
            record_token_usage(
                self.db,
                provider_id=provider.id,
                provider_name_snapshot=provider.provider_name,
                scene="qa_answer",
                usage=gateway.get_last_usage(),
            )
            return answer_text or ""
        except Exception as e:
            raise QAProviderInvokeError(f"Answer generation failed: {e}") from e

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _write_log(
        self,
        question: str,
        answer_text: str,
        query_plan: Any,
        events: list[EventEvidence],
        provider_id: int,
        provider_name_snapshot: str | None,
    ) -> None:
        import dataclasses

        try:
            plan_dict = dataclasses.asdict(query_plan)
            # datetime 不能直接 JSON 序列化，转成 ISO 字符串
            if plan_dict.get("time_range"):
                tr = plan_dict["time_range"]
                if isinstance(tr.get("start"), datetime):
                    tr["start"] = tr["start"].isoformat()
                if isinstance(tr.get("end"), datetime):
                    tr["end"] = tr["end"].isoformat()
        except Exception:
            plan_dict = None

        log = ChatQueryLog(
            user_question=question,
            parsed_condition_json=plan_dict,
            answer_text=answer_text,
            referenced_event_ids_json=[e.id for e in events],
            provider_id=provider_id,
            provider_name_snapshot=provider_name_snapshot,
        )
        self.db.add(log)
        self.db.commit()

    def _write_agent_log(
        self,
        question: str,
        answer_text: str,
        tool_calls_log: list[dict[str, Any]],
        provider_id: int,
        provider_name_snapshot: str | None,
    ) -> None:
        log = ChatQueryLog(
            user_question=question,
            parsed_condition_json={"mode": "agent", "tool_calls": tool_calls_log},
            answer_text=answer_text,
            referenced_event_ids_json=[],
            provider_id=provider_id,
            provider_name_snapshot=provider_name_snapshot,
        )
        self.db.add(log)
        self.db.commit()
