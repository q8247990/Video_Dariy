from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser, Locale
from src.schemas.dashboard import DashboardOverviewResponse
from src.schemas.response import BaseResponse
from src.services.dashboard import get_dashboard_overview

router = APIRouter()


@router.get("/overview", response_model=BaseResponse[DashboardOverviewResponse])
def get_overview(db: DB, current_user: CurrentUser, locale: Locale) -> Any:
    overview = get_dashboard_overview(db, locale=locale)
    return BaseResponse(data=overview)
