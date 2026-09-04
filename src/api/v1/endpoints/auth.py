from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, Locale
from src.core.i18n import t
from src.core.security import create_access_token, get_password_hash, verify_password
from src.models.admin_user import AdminUser
from src.schemas.auth import UserInit, UserLogin
from src.schemas.response import BaseResponse
from src.schemas.user import TokenResponse, UserResponse

router = APIRouter()


@router.post("/init", response_model=BaseResponse[dict])
def init_admin(db: DB, locale: Locale, data: UserInit) -> Any:
    user = db.query(AdminUser).first()
    if user:
        return BaseResponse(code=4001, message=t("auth.admin_already_init", locale))

    new_user = AdminUser(username=data.username, password_hash=get_password_hash(data.password))
    db.add(new_user)
    db.commit()
    return BaseResponse(data={"initialized": True})


@router.post("/login", response_model=BaseResponse[TokenResponse])
def login(db: DB, locale: Locale, data: UserLogin) -> Any:
    user = db.query(AdminUser).filter(AdminUser.username == data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        return BaseResponse(code=4011, message=t("auth.invalid_credentials", locale))

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    token = create_access_token(subject=str(user.id))
    return BaseResponse(
        data=TokenResponse(token=token, user=UserResponse(id=user.id, username=user.username))
    )
