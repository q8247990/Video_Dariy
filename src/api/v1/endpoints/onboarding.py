from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.schemas.onboarding import OnboardingStatusResponse
from src.schemas.response import BaseResponse
from src.services.onboarding import get_onboarding_status

router = APIRouter()


@router.get("/status", response_model=BaseResponse[OnboardingStatusResponse])
def get_status(db: DB, current_user: CurrentUser) -> Any:
    status = get_onboarding_status(db)
    return BaseResponse(data=OnboardingStatusResponse.model_validate(status))
