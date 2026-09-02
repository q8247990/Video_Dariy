"""Provider connectivity tester (services layer).

This module wraps a single ``LLMGatewayFactoryPort.build(...)`` call to
verify that an :class:`LLMProvider` row actually responds. It is a thin
domain helper — the real network round-trip lives behind the gateway
port owned by the composition root (``src.application.bootstrap``).

Architectural rule (Todo 5/8): **this module must not import
``src.infrastructure.*``** — concrete adapter binding is the composition
root's job. The :class:`LLMGatewayFactoryPort` is therefore an injected
dependency that callers (FastAPI endpoint, Celery task, integration
test) supply from outside. The function still works without an injected
factory: callers that build their own gateways can continue to use the
helper, but the **default** path is "ask the caller".

Historical behaviour note: the previous version used
``OpenAICompatGatewayFactory()`` directly here, which forced every test
to ``@patch("src.services.llm_provider_tester.OpenAICompatGatewayFactory")``.
The refactor moves that responsibility to the composition root so unit
tests can wire a :class:`FakeLLMGatewayFactory` instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.application.ports.llm_gateway import LLMGatewayFactoryPort
from src.core.i18n import DEFAULT_LOCALE, t
from src.models.llm_provider import LLMProvider
from src.services.provider_key_crypto import decrypt_provider_api_key

logger = logging.getLogger(__name__)


@dataclass
class ProviderTestResult:
    success: bool
    message: str
    supports_vision: bool
    supports_tool_calling: bool


def check_provider_connectivity(
    provider: LLMProvider,
    *,
    locale: Optional[str] = None,
    llm_factory: Optional[LLMGatewayFactoryPort] = None,
) -> ProviderTestResult:
    """Probe ``provider`` and report connectivity / capability flags.

    Args:
        provider: ORM model row representing the provider to test.
        locale: Locale used for the success / failure message. Defaults to
            :data:`src.core.i18n.DEFAULT_LOCALE`.
        llm_factory: :class:`LLMGatewayFactoryPort` to build the probe
            gateway from. Composition-root callers (the production
            FastAPI endpoint) pass ``container.llm_factory``; tests can
            pass a :class:`~src.application.bootstrap_fakes.FakeLLMGatewayFactory`
            to avoid any network round-trip.

    Returns:
        :class:`ProviderTestResult` carrying the connectivity verdict,
        localised message and the two capability flags.
    """

    loc = locale or DEFAULT_LOCALE
    test_status = "failed"
    test_message = ""
    vision_result = False
    tool_calling_result = False

    if llm_factory is None:
        # Defensive: rather than silently reaching for the production
        # adapter (which would re-introduce the very dependency this
        # module was just cleaned up to remove), surface a clear error
        # so callers know they forgot to wire the factory. The previous
        # behaviour was an implicit "production default"; explicit is
        # better than implicit.
        raise TypeError(
            "check_provider_connectivity requires an llm_factory "
            "(LLMGatewayFactoryPort) supplied by the caller; "
            "production callers must pass container.llm_factory."
        )

    api_key = decrypt_provider_api_key(provider.api_key)
    gateway = llm_factory.build(
        api_base_url=provider.api_base_url,
        api_key=api_key,
        model_name=provider.model_name,
        timeout_seconds=provider.timeout_seconds,
    )
    try:
        _ = gateway.chat_completion(
            messages=[
                {"role": "system", "content": "You are a connectivity test assistant."},
                {"role": "user", "content": "Reply with 'pong'."},
            ],
            temperature=0,
        )
        test_status = "success"
        test_message = "provider reachable"
    except Exception as e:
        test_message = str(e)[:512]
    else:
        vision_result = gateway.probe_vision()
        tool_calling_result = gateway.probe_tool_calling()

        capabilities = []
        if vision_result:
            capabilities.append(t("provider.test.cap_vision", loc))
        if tool_calling_result:
            capabilities.append(t("provider.test.cap_tool_calling", loc))
        cap_text = "\u3001".join(capabilities) if capabilities else t("provider.test.cap_none", loc)
        test_message = t("provider.test.reachable", loc, capabilities=cap_text)
    finally:
        gateway.close()

    return ProviderTestResult(
        success=(test_status == "success"),
        message=test_message,
        supports_vision=vision_result,
        supports_tool_calling=tool_calling_result,
    )
