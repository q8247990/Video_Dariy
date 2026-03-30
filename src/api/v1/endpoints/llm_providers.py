from datetime import datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy.exc import IntegrityError

from src.api.deps import DB, CurrentUser
from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
from src.models.llm_provider import LLMProvider
from src.providers.openai_client import OpenAIClient
from src.schemas.llm_provider import (
    LLMProviderCreate,
    LLMProviderResponse,
    LLMProviderUpdate,
    LLMProviderUsageDailyItem,
)
from src.schemas.response import BaseResponse, PaginatedData, PaginatedResponse, PaginationDetails
from src.services.llm_qos import get_daily_usage_stats, provider_availability
from src.services.provider_selector import (
    PROVIDER_TYPE_QA,
    PROVIDER_TYPE_VISION,
    capability_field_for_provider_type,
    find_enabled_provider,
)

router = APIRouter()


def _ensure_provider_capabilities(payload: dict[str, Any]) -> tuple[bool, bool]:
    supports_vision = bool(payload.get("supports_vision", False))
    supports_qa = bool(payload.get("supports_qa", True))

    if not supports_vision and not supports_qa:
        raise ValueError("Provider 至少需要开启一种能力")

    return supports_vision, supports_qa


def _apply_legacy_fields(payload: dict[str, Any]) -> None:
    supports_vision = bool(payload.get("supports_vision", False))
    supports_qa = bool(payload.get("supports_qa", True))
    payload["provider_type"] = (
        PROVIDER_TYPE_VISION if supports_vision and not supports_qa else PROVIDER_TYPE_QA
    )


@router.get("", response_model=PaginatedResponse[LLMProviderResponse])
def get_providers(
    db: DB,
    current_user: CurrentUser,
    page: int = 1,
    page_size: int = 20,
    provider_type: str | None = None,
    enabled: bool | None = None,
) -> Any:
    query = db.query(LLMProvider)
    if provider_type:
        try:
            capability_field = capability_field_for_provider_type(provider_type)
        except ValueError as e:
            return PaginatedResponse(
                code=4001,
                message=str(e),
                data=PaginatedData(
                    list=[],
                    pagination=PaginationDetails(page=page, page_size=page_size, total=0),
                ),
            )
        capability_column = getattr(LLMProvider, capability_field)
        query = query.filter(capability_column.is_(True))
    if enabled is not None:
        query = query.filter(LLMProvider.enabled == enabled)

    total = query.count()
    providers = query.offset((page - 1) * page_size).limit(page_size).all()

    payload_list: list[LLMProviderResponse] = []
    for provider in providers:
        availability_status, availability_message = provider_availability(provider)
        payload_list.append(
            LLMProviderResponse.model_validate(
                {
                    **provider.__dict__,
                    "availability_status": availability_status,
                    "availability_message": availability_message,
                }
            )
        )

    return PaginatedResponse(
        data=PaginatedData(
            list=payload_list,
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )


@router.post("", response_model=BaseResponse[LLMProviderResponse])
def create_provider(db: DB, current_user: CurrentUser, data: LLMProviderCreate) -> Any:
    dump = data.model_dump()
    try:
        supports_vision, supports_qa = _ensure_provider_capabilities(dump)
    except ValueError as e:
        return BaseResponse(code=4001, message=str(e))

    if not supports_vision:
        dump["is_default_vision"] = False
    if not supports_qa:
        dump["is_default_qa"] = False

    if dump.get("is_default_vision"):
        dump["supports_vision"] = True
        dump["enabled"] = True

    if dump.get("is_default_qa"):
        dump["supports_qa"] = True
        dump["enabled"] = True

    _apply_legacy_fields(dump)

    provider = LLMProvider(**dump)
    db.add(provider)
    db.flush()

    if provider.is_default_vision:
        db.query(LLMProvider).filter(
            LLMProvider.supports_vision.is_(True),
            LLMProvider.id != provider.id,
        ).update({"is_default_vision": False})

    if provider.is_default_qa:
        db.query(LLMProvider).filter(
            LLMProvider.supports_qa.is_(True),
            LLMProvider.id != provider.id,
        ).update({"is_default_qa": False})

    db.commit()
    db.refresh(provider)
    return BaseResponse(data=LLMProviderResponse.model_validate(provider))


@router.put("/{id}", response_model=BaseResponse[LLMProviderResponse])
def update_provider(  # noqa: C901
    db: DB, current_user: CurrentUser, id: int, data: LLMProviderUpdate
) -> Any:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if not provider:
        return BaseResponse(code=4002, message="Provider not found")

    dump = data.model_dump(exclude_unset=True)

    next_supports_vision = bool(dump.get("supports_vision", provider.supports_vision))
    next_supports_qa = bool(dump.get("supports_qa", provider.supports_qa))
    if not next_supports_vision and not next_supports_qa:
        return BaseResponse(code=4001, message="Provider 至少需要开启一种能力")

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
        return BaseResponse(code=4003, message="Cannot disable default provider")

    final_supports_vision = bool(dump.get("supports_vision", provider.supports_vision))
    final_supports_qa = bool(dump.get("supports_qa", provider.supports_qa))
    dump["provider_type"] = (
        PROVIDER_TYPE_VISION
        if final_supports_vision and not final_supports_qa
        else PROVIDER_TYPE_QA
    )

    for key, value in dump.items():
        setattr(provider, key, value)

    db.flush()
    if provider.is_default_vision:
        db.query(LLMProvider).filter(
            LLMProvider.supports_vision.is_(True),
            LLMProvider.id != provider.id,
        ).update({"is_default_vision": False})

    if provider.is_default_qa:
        db.query(LLMProvider).filter(
            LLMProvider.supports_qa.is_(True),
            LLMProvider.id != provider.id,
        ).update({"is_default_qa": False})

    db.commit()
    db.refresh(provider)
    return BaseResponse(data=LLMProviderResponse.model_validate(provider))


@router.delete("/{id}", response_model=BaseResponse[dict])
def delete_provider(db: DB, current_user: CurrentUser, id: int) -> Any:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if provider:
        if provider.is_default_vision or provider.is_default_qa:
            return BaseResponse(code=4003, message="Cannot delete default provider")

        active_vision = find_enabled_provider(db, PROVIDER_TYPE_VISION)
        active_qa = find_enabled_provider(db, PROVIDER_TYPE_QA)
        in_use_roles: list[str] = []
        if active_vision and active_vision.id == provider.id:
            in_use_roles.append("vision")
        if active_qa and active_qa.id == provider.id:
            in_use_roles.append("qa")

        if in_use_roles:
            role_text = ", ".join(in_use_roles)
            return BaseResponse(
                code=4004,
                message=f"Provider is currently in use by: {role_text}",
            )
        try:
            db.delete(provider)
            db.commit()
        except IntegrityError:
            db.rollback()
            return BaseResponse(
                code=4005,
                message="Provider has historical references and cannot be deleted",
            )
    return BaseResponse(data={})


@router.get("/usage/daily", response_model=BaseResponse[list[LLMProviderUsageDailyItem]])
def get_provider_daily_usage(db: DB, current_user: CurrentUser, days: int = 7) -> Any:
    safe_days = min(max(days, 1), 30)
    items = get_daily_usage_stats(db, days=safe_days)
    return BaseResponse(data=[LLMProviderUsageDailyItem.model_validate(item) for item in items])


@router.post("/{id}/enable", response_model=BaseResponse[dict])
def enable_provider(db: DB, current_user: CurrentUser, id: int) -> Any:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if provider:
        provider.enabled = True
        db.commit()
    return BaseResponse(data={})


@router.post("/{id}/disable", response_model=BaseResponse[dict])
def disable_provider(db: DB, current_user: CurrentUser, id: int) -> Any:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if provider:
        if provider.is_default_vision or provider.is_default_qa:
            return BaseResponse(code=4003, message="Cannot disable default provider")
        provider.enabled = False
        db.commit()
    return BaseResponse(data={})


@router.post("/{id}/set-default-vision", response_model=BaseResponse[dict])
def set_default_vision_provider(db: DB, current_user: CurrentUser, id: int) -> Any:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if not provider:
        return BaseResponse(code=4002, message="Provider not found")
    if not provider.supports_vision:
        return BaseResponse(code=4004, message="Provider does not support vision")

    db.query(LLMProvider).filter(
        LLMProvider.supports_vision.is_(True), LLMProvider.id != id
    ).update({"is_default_vision": False})

    provider.is_default_vision = True
    provider.enabled = True
    provider.provider_type = PROVIDER_TYPE_VISION if not provider.supports_qa else PROVIDER_TYPE_QA
    db.commit()
    return BaseResponse(data={})


@router.post("/{id}/set-default-qa", response_model=BaseResponse[dict])
def set_default_qa_provider(db: DB, current_user: CurrentUser, id: int) -> Any:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if not provider:
        return BaseResponse(code=4002, message="Provider not found")
    if not provider.supports_qa:
        return BaseResponse(code=4004, message="Provider does not support QA")

    db.query(LLMProvider).filter(LLMProvider.supports_qa.is_(True), LLMProvider.id != id).update(
        {"is_default_qa": False}
    )

    provider.is_default_qa = True
    provider.enabled = True
    provider.provider_type = PROVIDER_TYPE_VISION if not provider.supports_qa else PROVIDER_TYPE_QA
    db.commit()
    return BaseResponse(data={})


@router.post("/{id}/test", response_model=BaseResponse[dict])
def test_provider(db: DB, current_user: CurrentUser, id: int) -> Any:
    provider = db.query(LLMProvider).filter(LLMProvider.id == id).first()
    if not provider:
        return BaseResponse(code=4002, message="Provider not found")

    test_status = "failed"
    test_message = ""
    vision_result = False
    tool_calling_result = False

    client = OpenAIClient(
        api_base_url=provider.api_base_url,
        api_key=provider.api_key,
        model_name=provider.model_name,
        timeout=provider.timeout_seconds,
    )

    # 1. 连通性测试
    try:
        gateway = OpenAICompatGatewayFactory().build(
            api_base_url=provider.api_base_url,
            api_key=provider.api_key,
            model_name=provider.model_name,
            timeout_seconds=provider.timeout_seconds,
        )
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

    # 2. 连通性通过后，探测视觉和 tool_calling 能力
    if test_status == "success":
        vision_result = client.probe_vision()
        tool_calling_result = client.probe_tool_calling()

        # 自动回写能力字段
        provider.supports_vision = vision_result
        provider.supports_tool_calling = tool_calling_result

        capabilities = []
        if vision_result:
            capabilities.append("视觉")
        if tool_calling_result:
            capabilities.append("工具调用")
        cap_text = "、".join(capabilities) if capabilities else "无"
        test_message = f"连通性正常，检测到能力：{cap_text}"

    provider.last_test_status = test_status
    provider.last_test_message = test_message
    provider.last_test_at = datetime.utcnow()
    db.commit()
    db.refresh(provider)

    return BaseResponse(
        data={
            "success": test_status == "success",
            "message": test_message,
            "last_test_status": provider.last_test_status,
            "last_test_message": provider.last_test_message,
            "last_test_at": provider.last_test_at,
            "supports_vision": provider.supports_vision,
            "supports_tool_calling": provider.supports_tool_calling,
        }
    )
