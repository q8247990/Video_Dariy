"""Application-layer use cases for the Home Profile endpoints.

The entity-appearance generation flow previously lived inside
:mod:`src.api.v1.endpoints.home_profile` and reached into the LLM
gateway adapter directly. That has been moved here behind the
:class:`~src.application.ports.llm_gateway.LLMGatewayFactoryPort`
provided by the composition root, so the endpoint layer stays free of
``src.infrastructure.*`` imports while the orchestration logic is
reusable from CLI / MCP entry points.

The use case returns a plain dataclass carrying the HTTP response code
(``0`` on success) and the resulting :class:`HomeEntityResponse` (or
``None`` on failure), letting the endpoint stay a thin adapter between
the dataclass and the FastAPI ``BaseResponse`` envelope.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy.orm import Session

from src.application.bootstrap import Container
from src.core.i18n import t
from src.models.home_entity_profile import HomeEntityProfile
from src.schemas.home_profile import HomeEntityResponse
from src.services.home_profile import get_entity_by_id
from src.services.provider_generation_limits import resolve_max_output_tokens
from src.services.provider_key_crypto import decrypt_provider_api_key
from src.services.provider_selector import PROVIDER_TYPE_VISION, find_enabled_provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerateEntityAppearanceResult:
    """Result of :func:`generate_entity_appearance_use_case`.

    ``error_code`` follows the legacy business-code convention:

    * ``0`` — success, ``entity`` carries the refreshed row
    * ``4002`` — entity not found
    * ``4000`` — image has not been uploaded yet
    * ``5000`` — vision provider not configured, image read failure
    * ``5002`` — LLM call failed or returned no content
    """

    error_code: int
    error_message: str
    entity: Optional[HomeEntityResponse] = None


def generate_entity_appearance_use_case(
    *,
    db: Session,
    entity_id: int,
    locale: str,
    container: Container,
    entity_image_path_resolver: Callable[[int], str],
    entity_response_builder: Callable[[HomeEntityProfile], HomeEntityResponse],
) -> GenerateEntityAppearanceResult:
    """Describe the visual appearance of a home entity via the LLM port.

    Mirrors the legacy :func:`src.api.v1.endpoints.home_profile.generate_entity_appearance`
    endpoint, but resolves the LLM gateway through
    :class:`~src.application.ports.llm_gateway.LLMGatewayFactoryPort` rather
    than constructing :class:`OpenAICompatGatewayFactory` directly. The
    prompt, temperature, ``max_tokens`` and the friendly error mapping
    are kept identical to preserve the existing HTTP body shapes and
    business error codes.
    """

    entity = get_entity_by_id(db, entity_id)
    if entity is None:
        return GenerateEntityAppearanceResult(
            error_code=4002,
            error_message=t("entity.not_found", locale),
        )

    image_path = entity_image_path_resolver(entity_id)
    if not entity.image_path or not _exists(image_path):
        return GenerateEntityAppearanceResult(
            error_code=4000,
            error_message=t("entity.upload_image_first", locale),
        )

    provider = find_enabled_provider(db, PROVIDER_TYPE_VISION)
    if provider is None:
        return GenerateEntityAppearanceResult(
            error_code=5000,
            error_message=t("entity.no_vision_provider", locale),
        )

    try:
        with open(image_path, "rb") as fp:
            image_b64 = base64.b64encode(fp.read()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{image_b64}"
    except OSError as exc:
        logger.error("Failed to read image for entity %s: %s", entity_id, exc)
        return GenerateEntityAppearanceResult(
            error_code=5000,
            error_message=t("entity.image_read_failed", locale),
        )

    entity_label = "宠物" if entity.entity_type == "pet" else "家庭成员"
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"请仔细观察图片中的{entity_label}，用中文描述其外观特征。"
                        f"描述应包括：体型、毛发/发型发色、面部特征、常见穿着风格等可观察到的外观信息。"
                        f"只描述外观，不要推测性格或行为。"
                        f"描述控制在 150 字以内，语言简洁自然。"
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]

    gateway = None
    try:
        gateway = container.llm_factory.build(
            api_base_url=provider.api_base_url,
            api_key=decrypt_provider_api_key(provider.api_key),
            model_name=provider.model_name,
            timeout_seconds=provider.timeout_seconds,
        )
        result = gateway.chat_completion(
            messages=messages,
            temperature=0.3,
            max_tokens=resolve_max_output_tokens(provider, scene_default=300),
        )
    except Exception as exc:  # noqa: BLE001 — surface provider errors as 5002
        logger.error("Vision LLM call failed for entity %s: %s", entity_id, exc)
        return GenerateEntityAppearanceResult(
            error_code=5002,
            error_message=t("entity.ai_generate_failed", locale, error=exc),
        )
    finally:
        if gateway is not None:
            gateway.close()

    if not result:
        return GenerateEntityAppearanceResult(
            error_code=5002,
            error_message=t("entity.ai_no_result", locale),
        )

    entity.appearance_desc = result.strip()
    return GenerateEntityAppearanceResult(
        error_code=0,
        error_message="",
        entity=entity_response_builder(entity),
    )


def _exists(path: str) -> bool:
    """Proxy for ``os.path.exists`` kept as a module-private helper so
    tests can monkeypatch the existence check without polluting the
    public surface.
    """

    import os

    return os.path.exists(path)


__all__ = ["GenerateEntityAppearanceResult", "generate_entity_appearance_use_case"]
