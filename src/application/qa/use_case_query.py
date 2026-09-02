"""QA application use case.

Wraps :class:`src.application.qa.service.QAService` so that callers
(``src.api`` endpoints, ``src.mcp`` tools, future CLI scripts) can drive
a QA round-trip through the composition-root
:class:`~src.application.bootstrap.Container` instead of constructing
an :class:`OpenAICompatGatewayFactory` ad-hoc. The factory instance is
stateless, so the use case accepts a caller-managed
:class:`~src.application.ports.llm_gateway.LLMGatewayFactoryPort` and
binds it once at construction time.

The use case owns **no** SQLAlchemy session: the caller passes the
request-scoped ``Session`` and is responsible for committing any log
rows the underlying service persists.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.application.bootstrap import Container
from src.application.qa.schemas import QARequest, QAResult
from src.application.qa.service import QAService


class AnswerQuestionUseCase:
    """Answer a free-form home-monitor question via the LLM gateway port.

    The use case holds a reference to the composition-root
    :class:`~src.application.bootstrap.Container` and a per-request
    SQLAlchemy ``Session``. It instantiates a
    :class:`~src.application.qa.service.QAService` bound to the
    container's ``llm_factory`` port on demand. Construction is cheap
    because the factory itself is stateless; multiple questions in the
    same request can share one use case instance safely.
    """

    def __init__(self, *, db: Session, container: Container):
        self.db = db
        self._container = container

    def execute(self, request: QARequest) -> QAResult:
        service = QAService(db=self.db, llm_factory=self._container.llm_factory)
        return service.answer(request)


__all__ = ["AnswerQuestionUseCase"]
