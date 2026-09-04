from typing import Any

from fastapi import APIRouter

from src.api.common import paginate
from src.api.deps import DB, ContainerDep, CurrentUser, Locale
from src.application.llm_providers import (
    create_provider_use_case,
    delete_provider_use_case,
    set_default_provider_use_case,
    test_provider_use_case,
    update_provider_use_case,
)
from src.models.llm_provider import LLMProvider
from src.schemas.llm_provider import (
    LLMProviderCreate,
    LLMProviderResponse,
    LLMProviderUpdate,
    LLMProviderUsageDailyItem,
)
from src.schemas.response import BaseResponse, PaginatedResponse
from src.services.llm_qos import get_daily_usage_stats, provider_availability
from src.services.provider_selector import (
    PROVIDER_TYPE_QA,
    PROVIDER_TYPE_VISION,
    capability_field_for_provider_type,
)

router = APIRouter()


def _provider_response_builder(provider: LLMProvider) -> dict[str, Any]:
    availability_status, availability_message = provider_availability(provider)
    return {
        **provider.__dict__,
        "availability_status": availability_status,
        "availability_message": availability_message,
    }


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
            return paginate(
                db.query(LLMProvider).filter(LLMProvider.id < 0),
                page=page,
                page_size=page_size,
                schema=LLMProviderResponse,
                transform=_provider_response_builder,
            ).model_copy(update={"code": 4001, "message": str(e)})
        capability_column = getattr(LLMProvider, capability_field)
        query = query.filter(capability_column.is_(True))
    if enabled is not None:
        query = query.filter(LLMProvider.enabled == enabled)

    return paginate(
        query,
        page=page,
        page_size=page_size,
        schema=LLMProviderResponse,
        transform=_provider_response_builder,
    )


@router.post("", response_model=BaseResponse[LLMProviderResponse])
def create_provider(
    db: DB, current_user: CurrentUser, locale: Locale, data: LLMProviderCreate
) -> Any:
    result = create_provider_use_case(db, data.model_dump(), locale)
    if result.error_code != 0:
        return BaseResponse(code=result.error_code, message=result.error_message)
    db.commit()
    db.refresh(result.provider)
    return BaseResponse(data=LLMProviderResponse.model_validate(result.provider))


@router.put("/{id}", response_model=BaseResponse[LLMProviderResponse])
def update_provider(
    db: DB, current_user: CurrentUser, locale: Locale, id: int, data: LLMProviderUpdate
) -> Any:
    result = update_provider_use_case(db, id, data.model_dump(exclude_unset=True), locale)
    if result.error_code != 0:
        return BaseResponse(code=result.error_code, message=result.error_message)
    db.commit()
    db.refresh(result.provider)
    return BaseResponse(data=LLMProviderResponse.model_validate(result.provider))


@router.delete("/{id}", response_model=BaseResponse[dict])
def delete_provider(db: DB, current_user: CurrentUser, locale: Locale, id: int) -> Any:
    result = delete_provider_use_case(db, id, locale)
    if result.error_code != 0:
        return BaseResponse(code=result.error_code, message=result.error_message)
    return BaseResponse(data={})


@router.get("/usage/daily", response_model=BaseResponse[list[LLMProviderUsageDailyItem]])
def get_provider_daily_usage(db: DB, current_user: CurrentUser, days: int = 7) -> Any:
    safe_days = min(max(days, 1), 30)
    items = get_daily_usage_stats(db, days=safe_days)
    return BaseResponse(data=[LLMProviderUsageDailyItem.model_validate(item) for item in items])


@router.post("/{id}/set-default-vision", response_model=BaseResponse[dict])
def set_default_vision_provider(db: DB, current_user: CurrentUser, locale: Locale, id: int) -> Any:
    result = set_default_provider_use_case(db, id, PROVIDER_TYPE_VISION, locale)
    if result.error_code != 0:
        return BaseResponse(code=result.error_code, message=result.error_message)
    db.commit()
    return BaseResponse(data={})


@router.post("/{id}/set-default-qa", response_model=BaseResponse[dict])
def set_default_qa_provider(db: DB, current_user: CurrentUser, locale: Locale, id: int) -> Any:
    result = set_default_provider_use_case(db, id, PROVIDER_TYPE_QA, locale)
    if result.error_code != 0:
        return BaseResponse(code=result.error_code, message=result.error_message)
    db.commit()
    return BaseResponse(data={})


@router.post("/{id}/test", response_model=BaseResponse[dict])
def test_provider(
    db: DB,
    current_user: CurrentUser,
    locale: Locale,
    id: int,
    container: ContainerDep,
) -> Any:
    result = test_provider_use_case(db, id, locale, container)
    if result.error_code != 0:
        return BaseResponse(code=result.error_code, message=result.error_message)
    db.commit()
    return BaseResponse(data=result.payload)
