from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.core.config import settings
from src.core.i18n import DEFAULT_LOCALE, reload_catalogs
from src.models.system_config import SystemConfig
from src.schemas.response import BaseResponse
from src.schemas.system_config import SystemConfigUpdate

router = APIRouter()


@router.get("", response_model=BaseResponse[dict])
def get_system_config(db: DB, current_user: CurrentUser) -> Any:
    configs = db.query(SystemConfig).all()
    result = {}
    for c in configs:
        result[c.config_key] = c.config_value
    mcp_token = result.get("mcp_token")
    if not isinstance(mcp_token, str) or not mcp_token.strip():
        result["mcp_token"] = settings.MCP_TOKEN
    if "default_locale" not in result:
        result["default_locale"] = DEFAULT_LOCALE
    return BaseResponse(data=result)


@router.put("", response_model=BaseResponse[dict])
def update_system_config(db: DB, current_user: CurrentUser, data: SystemConfigUpdate) -> Any:
    updates = data.model_dump(exclude_unset=True)
    if "daily_summary_time" in updates and "daily_summary_schedule" not in updates:
        updates["daily_summary_schedule"] = updates["daily_summary_time"]

    locale_changed = "default_locale" in updates

    for key, value in updates.items():
        if key == "daily_summary_time":
            continue
        config = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
        if config:
            config.config_value = value
        else:
            db.add(SystemConfig(config_key=key, config_value=value))

    db.commit()

    if locale_changed:
        reload_catalogs()

    return get_system_config(db=db, current_user=current_user)
