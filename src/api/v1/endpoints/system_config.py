from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.core.i18n import DEFAULT_LOCALE, reload_catalogs
from src.schemas.response import BaseResponse
from src.schemas.system_config import SystemConfigUpdate
from src.services.system_config_registry import (
    DEFAULT_LOCALE as DEFAULT_LOCALE_KEY,
)
from src.services.system_config_registry import (
    MCP_TOKEN,
    REGISTRY,
    SystemConfigValidationError,
    get_config,
    set_config,
    validate_updates,
)

router = APIRouter()


@router.get("", response_model=BaseResponse[dict])
def get_system_config(db: DB, current_user: CurrentUser) -> Any:
    result = {key: get_config(db, key) for key in REGISTRY}
    if not result[MCP_TOKEN]:
        from src.core.config import settings

        result[MCP_TOKEN] = settings.MCP_TOKEN
    if not result[DEFAULT_LOCALE_KEY]:
        result[DEFAULT_LOCALE_KEY] = DEFAULT_LOCALE
    return BaseResponse(data=result)


@router.put("", response_model=BaseResponse[dict])
def update_system_config(db: DB, current_user: CurrentUser, data: SystemConfigUpdate) -> Any:
    try:
        updates = validate_updates(data.model_dump(exclude_unset=True))
    except SystemConfigValidationError as error:
        return BaseResponse(code=4000, message=str(error))
    locale_changed = DEFAULT_LOCALE_KEY in updates
    for key, value in updates.items():
        set_config(db, key, value)

    db.commit()

    if locale_changed:
        reload_catalogs()

    return get_system_config(db=db, current_user=current_user)
