"""Provider connectivity tester (services layer).

This module wraps a single ``LLMGatewayFactoryPort.build(...)`` call to
verify that an :class:`LLMProvider` row actually responds. It is a thin
domain helper — the real network round-trip lives behind the gateway
port owned by the composition root (``src.application.bootstrap``).

Architectural rule: **this module must not import
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
from typing import Any

from src.application.ports.llm_gateway import LLMGatewayFactoryPort
from src.core.i18n import DEFAULT_LOCALE, t
from src.core.provider_key_crypto import decrypt_provider_api_key
from src.models.llm_provider import LLMProvider
from src.services.frame_sampling.budget import (
    FramePlan,
    FramePlanImpossible,
    compute_frame_plan,
)
from src.services.frame_sampling.mode import INPUT_MODE_IMAGE, resolve_input_mode
from src.services.frame_sampling.policy import parse_frame_policy
from src.services.frame_sampling.tokens import estimate_image_tokens, fit_long_edge
from src.services.provider_generation_limits import (
    get_generation_limits,
    resolve_image_output_tokens,
)

logger = logging.getLogger(__name__)

# Representative worst-case clip used to validate the image frame plan at
# provider create / update / test time. One typical surveillance file (60 s of
# 640x480 source) with an 800-token analysis prompt is the largest realistic
# single vision call in this pipeline (one file = one sub-chunk), so a plan
# that cannot afford it will also fail on real files — reject it up front
# instead of letting the first analysis run burn the context window.
REPRESENTATIVE_CLIP_SECONDS: float = 60.0
REPRESENTATIVE_SOURCE_WIDTH: int = 640
REPRESENTATIVE_SOURCE_HEIGHT: int = 480
REPRESENTATIVE_PROMPT_TOKENS: int = 800


@dataclass
class ProviderTestResult:
    success: bool
    message: str
    supports_vision: bool
    supports_tool_calling: bool


def check_image_frame_plan(provider: Any) -> FramePlan | None:
    """Compute the representative image-mode frame plan for ``provider``.

    Video-mode providers (no ``frame_sampling`` config) return ``None`` without
    touching the plan. Image-mode providers must carry a configured
    ``max_model_len`` — a default context is never assumed. Raises
    :class:`FramePlanImpossible` when the token budget cannot afford
    ``min_frames`` images and ``ValueError`` for malformed parameters.
    """
    if resolve_input_mode(provider) != INPUT_MODE_IMAGE:
        return None
    limits = get_generation_limits(provider)
    if limits.max_model_len is None:
        raise ValueError(
            "image frame sampling requires max_model_len: fill "
            "extra_config_json.max_model_len with the model's total context "
            "window (input + output) in tokens; a default is never assumed"
        )
    policy = parse_frame_policy(provider)
    max_output_tokens = resolve_image_output_tokens(provider)
    return compute_frame_plan(
        context_size=limits.max_model_len,
        policy=policy,
        duration_seconds=REPRESENTATIVE_CLIP_SECONDS,
        prompt_tokens_est=REPRESENTATIVE_PROMPT_TOKENS,
        frame_width=REPRESENTATIVE_SOURCE_WIDTH,
        frame_height=REPRESENTATIVE_SOURCE_HEIGHT,
        max_output_tokens=max_output_tokens,
    )


def frame_plan_error_message(provider: Any, error: Exception) -> str:
    """Operator-facing message for a failed image frame-plan check.

    ``FramePlanImpossible`` lists the computed numbers and exactly three
    remedies (raise ``max_model_len``, lower ``max_output_tokens``, lower
    ``min_frames``); any other ``ValueError`` carries its original message.
    """
    if isinstance(error, FramePlanImpossible):
        context_size = get_generation_limits(provider).max_model_len
        policy = parse_frame_policy(provider)
        max_output_tokens = resolve_image_output_tokens(provider)
        fitted = fit_long_edge(
            REPRESENTATIVE_SOURCE_WIDTH, REPRESENTATIVE_SOURCE_HEIGHT, policy.max_edge
        )
        tokens_per_image = estimate_image_tokens(fitted[0], fitted[1])
        usable = int(context_size * policy.target_context_ratio)
        budget_tokens = usable - REPRESENTATIVE_PROMPT_TOKENS - max_output_tokens
        return (
            "image frame plan cannot fit the model context: "
            f"budget_tokens={budget_tokens} < min_frames*tokens_per_image="
            f"{policy.min_frames * tokens_per_image} "
            f"(max_model_len={context_size}, target_context_ratio={policy.target_context_ratio}, "
            f"prompt_tokens_est={REPRESENTATIVE_PROMPT_TOKENS}, "
            f"max_output_tokens={max_output_tokens}, min_frames={policy.min_frames}, "
            f"tokens_per_image={tokens_per_image}); "
            "remedies: raise max_model_len (context), lower max_output_tokens, "
            "or lower min_frames"
        )
    return str(error)


def _image_frame_plan_failure_message(provider: Any) -> str | None:
    """``None`` when the provider is video-mode or its representative plan fits."""
    try:
        check_image_frame_plan(provider)
    except ValueError as exc:  # includes FramePlanImpossible
        logger.warning(
            "image frame plan check failed for provider %s: %s",
            getattr(provider, "id", None),
            exc,
        )
        return frame_plan_error_message(provider, exc)
    return None


def check_provider_connectivity(
    provider: LLMProvider,
    *,
    locale: str | None = None,
    llm_factory: LLMGatewayFactoryPort | None = None,
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

    # Fail fast before any network round-trip: an image-mode provider whose
    # representative plan (N frames x tokens_per_image + prompt + output)
    # cannot fit max_model_len would fail on every real analysis call.
    plan_error = _image_frame_plan_failure_message(provider)
    if plan_error is not None:
        return ProviderTestResult(
            success=False,
            message=plan_error,
            supports_vision=False,
            supports_tool_calling=False,
        )

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
