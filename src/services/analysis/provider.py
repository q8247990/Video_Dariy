"""LLM provider client factory for the analyzer pipeline.

The factory is injected by the slim Celery task from the composition
root's :class:`Container.llm_factory`; this module never imports
``src.infrastructure.*`` directly (which would violate the
``services_no_infrastructure_import`` boundary rule). Tests can
substitute a fake by binding a different factory in the container
they pass to ``bootstrap_for_tests``.

Decryption of the stored provider API key happens here, not in the
sub-chunk runner, so the runner remains a pure function of
``client`` / ``provider`` and can be unit-tested without touching
the cryptographic layer.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.application.ports.llm_gateway import LLMGatewayFactoryPort, LLMGatewayPort
from src.models.llm_provider import LLMProvider
from src.services.provider_key_crypto import decrypt_provider_api_key
from src.services.provider_selector import (
    PROVIDER_TYPE_VISION,
    find_required_enabled_provider,
)


def build_provider_client(
    db: Session,
    *,
    llm_factory: LLMGatewayFactoryPort,
) -> tuple[LLMGatewayPort, LLMProvider]:
    """Resolve the default vision provider and build a fresh gateway.

    The factory is stateless; ``build`` returns a new
    :class:`LLMGatewayPort` (and underlying ``OpenAIClient``) per
    call, which the slim Celery task closes in its error branch.
    """
    provider = find_required_enabled_provider(db, PROVIDER_TYPE_VISION)
    client = llm_factory.build(
        api_base_url=provider.api_base_url,
        api_key=decrypt_provider_api_key(provider.api_key),
        model_name=provider.model_name,
        timeout_seconds=provider.timeout_seconds,
    )
    return client, provider


def _build_provider_client(
    db: Session, *, llm_factory: LLMGatewayFactoryPort | None = None
) -> tuple[Any, LLMProvider]:
    """Build the vision provider client for the analyzer orchestration.

    ``llm_factory`` defaults to ``None`` so unit tests that monkey-patch
    ``src.tasks._analyzer_orchestration._build_provider_client`` with
    the legacy single-arg lambda keep working unchanged; in production
    we read the factory from the task-layer ``get_container()``
    singleton so the composition root owns the adapter binding.
    """
    if llm_factory is None:
        from src.tasks._container import get_container

        llm_factory = get_container().llm_factory
    return build_provider_client(db, llm_factory=llm_factory)


__all__ = [
    "_build_provider_client",
    "build_provider_client",
]
