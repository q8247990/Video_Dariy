"""Application-layer use cases for asynchronous task lifecycle endpoints.

The endpoints under :mod:`src.api.v1.endpoints.tasks` previously reached
into Celery (``src.core.celery_app``) and the Celery-backed dispatcher
directly. This package wraps those flows behind use cases that depend
only on the composition-root :class:`~src.application.bootstrap.Container`
and a caller-supplied SQLAlchemy ``Session`` — so the API layer is free
of infrastructure imports while the orchestration logic is reusable from
other entry points (CLI scripts, MCP tooling) in the future.

The use cases return plain dataclasses that the endpoint layer turns
into HTTP responses. Endpoints are responsible for committing the
``Session`` once the use case reports success — this keeps session
ownership with the caller (FastAPI dependency) and prevents use cases
from holding ORM sessions across calls.
"""

from src.application.tasks.use_case_stop_retry import (
    RetryTaskLogResult,
    StopTaskLogResult,
    retry_task_log_use_case,
    stop_task_log_use_case,
)

__all__ = [
    "RetryTaskLogResult",
    "StopTaskLogResult",
    "retry_task_log_use_case",
    "stop_task_log_use_case",
]
