from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.application.bootstrap import Container
from src.core.i18n import t
from src.models.llm_provider import LLMProvider
from src.services.llm_provider_tester import check_provider_connectivity
from src.services.provider_key_crypto import encrypt_provider_api_key
from src.services.provider_selector import (
    PROVIDER_TYPE_QA,
    PROVIDER_TYPE_VISION,
    capability_field_for_provider_type,
    default_field_for_provider_type,
    find_enabled_provider,
    reset_other_default_providers,
)

_VISION = "vision"
_QA = "qa"
_DEFAULT_MSG_KEY_BY_TYPE = {
    PROVIDER_TYPE_VISION: "provider.not_support_vision",
    PROVIDER_TYPE_QA: "provider.not_support_qa",
}


@dataclass
class ProviderMutationResult:
    error_code: int = 0
    error_message: str = ""
    provider: Optional[LLMProvider] = None
    payload: Optional[dict] = None


def _err(code: int, message: str) -> ProviderMutationResult:
    return ProviderMutationResult(error_code=code, error_message=message)


def _ok(
    provider: Optional[LLMProvider] = None, payload: Optional[dict] = None
) -> ProviderMutationResult:
    return ProviderMutationResult(provider=provider, payload=payload)


def _ensure_provider_capabilities(dump: dict, locale: str) -> None:
    supports_vision = bool(dump.get("supports_vision", False))
    supports_qa = bool(dump.get("supports_qa", True))
    if not supports_vision and not supports_qa:
        raise ValueError(t("provider.capability_required", locale))


def _apply_legacy_fields(dump: dict) -> None:
    supports_vision = bool(dump.get("supports_vision", False))
    supports_qa = bool(dump.get("supports_qa", True))
    dump["provider_type"] = (
        PROVIDER_TYPE_VISION if supports_vision and not supports_qa else PROVIDER_TYPE_QA
    )


def _set_provider_type_flag(dump: dict, provider: LLMProvider) -> None:
    final_supports_vision = bool(dump.get("supports_vision", provider.supports_vision))
    final_supports_qa = bool(dump.get("supports_qa", provider.supports_qa))
    dump["provider_type"] = (
        PROVIDER_TYPE_VISION
        if final_supports_vision and not final_supports_qa
        else PROVIDER_TYPE_QA
    )


def create_provider_use_case(db: Session, dump: dict, locale: str) -> ProviderMutationResult:
    try:
        _ensure_provider_capabilities(dump, locale)
    except ValueError as exc:
        return _err(4001, str(exc))

    if not bool(dump.get("supports_vision", False)):
        dump["is_default_vision"] = False
    if not bool(dump.get("supports_qa", True)):
        dump["is_default_qa"] = False

    if dump.get("is_default_vision"):
        dump["supports_vision"] = True
        dump["enabled"] = True

    if dump.get("is_default_qa"):
        dump["supports_qa"] = True
        dump["enabled"] = True

    _apply_legacy_fields(dump)
    dump["api_key"] = encrypt_provider_api_key(dump["api_key"])

    provider = LLMProvider(**dump)
    db.add(provider)
    db.flush()
    reset_other_default_providers(db, provider)
    return _ok(provider=provider)


def update_provider_use_case(
    db: Session, id: int, dump: dict, locale: str
) -> ProviderMutationResult:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if not provider:
        return _err(4002, t("provider.not_found", locale))

    if "api_key" in dump and dump["api_key"] is not None:
        dump["api_key"] = encrypt_provider_api_key(dump["api_key"])

    next_supports_vision = bool(dump.get("supports_vision", provider.supports_vision))
    next_supports_qa = bool(dump.get("supports_qa", provider.supports_qa))
    if not next_supports_vision and not next_supports_qa:
        return _err(4001, t("provider.capability_required", locale))

    next_is_default_vision = bool(dump.get("is_default_vision", provider.is_default_vision))
    next_is_default_qa = bool(dump.get("is_default_qa", provider.is_default_qa))

    if not next_supports_vision:
        dump["is_default_vision"] = False
    if not next_supports_qa:
        dump["is_default_qa"] = False

    if next_is_default_vision:
        dump["supports_vision"] = True
        dump["enabled"] = True

    if next_is_default_qa:
        dump["supports_qa"] = True
        dump["enabled"] = True

    would_be_default = (next_is_default_vision and next_supports_vision) or (
        next_is_default_qa and next_supports_qa
    )
    if would_be_default and dump.get("enabled") is False:
        return _err(4003, t("provider.cannot_disable_default", locale))

    _set_provider_type_flag(dump, provider)
    for key, value in dump.items():
        setattr(provider, key, value)
    db.flush()
    reset_other_default_providers(db, provider)
    return _ok(provider=provider)


def delete_provider_use_case(db: Session, id: int, locale: str) -> ProviderMutationResult:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if provider is None:
        return _ok()

    if provider.is_default_vision or provider.is_default_qa:
        return _err(4003, t("provider.cannot_delete_default", locale))

    active_vision = find_enabled_provider(db, PROVIDER_TYPE_VISION)
    active_qa = find_enabled_provider(db, PROVIDER_TYPE_QA)
    in_use_roles: list[str] = []
    if active_vision and active_vision.id == provider.id:
        in_use_roles.append(_VISION)
    if active_qa and active_qa.id == provider.id:
        in_use_roles.append(_QA)

    if in_use_roles:
        role_text = ", ".join(in_use_roles)
        return _err(4004, t("provider.in_use", locale, roles=role_text))

    try:
        db.delete(provider)
        db.commit()
    except IntegrityError:
        db.rollback()
        return _err(4005, t("provider.has_references", locale))
    return _ok()


def set_default_provider_use_case(
    db: Session, id: int, provider_type: str, locale: str
) -> ProviderMutationResult:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if not provider:
        return _err(4002, t("provider.not_found", locale))

    capability_field = capability_field_for_provider_type(provider_type)
    default_field = default_field_for_provider_type(provider_type)
    capability_column = getattr(LLMProvider, capability_field)

    if not getattr(provider, capability_field):
        return _err(4004, t(_DEFAULT_MSG_KEY_BY_TYPE[provider_type], locale))

    db.query(LLMProvider).filter(capability_column.is_(True), LLMProvider.id != id).update(
        {default_field: False}
    )

    setattr(provider, default_field, True)
    provider.enabled = True
    provider.provider_type = PROVIDER_TYPE_VISION if not provider.supports_qa else PROVIDER_TYPE_QA
    return _ok(provider=provider)


def enable_provider_use_case(db: Session, id: int) -> ProviderMutationResult:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if provider:
        provider.enabled = True
    return _ok(provider=provider)


def disable_provider_use_case(db: Session, id: int, locale: str) -> ProviderMutationResult:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if provider:
        if provider.is_default_vision or provider.is_default_qa:
            return _err(4003, t("provider.cannot_disable_default", locale))
        provider.enabled = False
    return _ok(provider=provider)


def test_provider_use_case(
    db: Session, id: int, locale: str, container: Container
) -> ProviderMutationResult:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if not provider:
        return _err(4002, t("provider.not_found", locale))

    result = check_provider_connectivity(
        provider,
        locale=locale,
        llm_factory=container.llm_factory,
    )

    provider.supports_vision = result.supports_vision
    provider.supports_tool_calling = result.supports_tool_calling
    provider.last_test_status = "success" if result.success else "failed"
    provider.last_test_message = result.message
    provider.last_test_at = datetime.now(timezone.utc)

    return _ok(
        payload={
            "success": result.success,
            "message": result.message,
            "last_test_status": provider.last_test_status,
            "last_test_message": provider.last_test_message,
            "last_test_at": provider.last_test_at,
            "supports_vision": provider.supports_vision,
            "supports_tool_calling": provider.supports_tool_calling,
        }
    )
