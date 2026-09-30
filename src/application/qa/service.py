import logging
from typing import Optional

from sqlalchemy.orm import Session

from src.application.ports.llm_gateway import LLMGatewayFactoryPort
from src.application.qa.agent_strategy import AgentQAStrategy
from src.application.qa.errors import QAProviderNotConfiguredError
from src.application.qa.schemas import QARequest, QAResult
from src.core.exceptions import DomainError
from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
from src.services.llm_qos import enforce_token_quota
from src.services.provider_key_crypto import decrypt_provider_api_key
from src.services.provider_selector import PROVIDER_TYPE_QA, find_enabled_provider

logger = logging.getLogger(__name__)

__all__ = ["QAService"]


class QAService:
    def __init__(
        self,
        db: Session,
        llm_factory: Optional[LLMGatewayFactoryPort] = None,
    ):
        self.db = db
        if llm_factory is None:
            llm_factory = OpenAICompatGatewayFactory()
        self._gateway_factory = llm_factory

    def answer(self, request: QARequest) -> QAResult:
        question = request.question.strip()
        if not question:
            raise DomainError("question is required")

        provider = find_enabled_provider(self.db, PROVIDER_TYPE_QA)
        if provider is None:
            raise QAProviderNotConfiguredError("No QA provider configured")
        if not provider.supports_tool_calling:
            raise QAProviderNotConfiguredError("QA provider does not support tool calling")
        enforce_token_quota(self.db, provider)

        gateway = self._gateway_factory.build(
            api_base_url=provider.api_base_url,
            api_key=decrypt_provider_api_key(provider.api_key),
            model_name=provider.model_name,
            timeout_seconds=provider.timeout_seconds,
            supports_tool_calling=provider.supports_tool_calling,
        )
        try:
            return AgentQAStrategy(self.db, gateway, provider).execute(question, request)
        finally:
            gateway.close()
