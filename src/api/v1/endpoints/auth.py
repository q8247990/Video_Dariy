from datetime import datetime
from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.core.security import create_access_token, get_password_hash, verify_password
from src.models.admin_user import AdminUser
from src.schemas.auth import UserChangePassword, UserInit, UserLogin
from src.schemas.response import BaseResponse
from src.schemas.user import TokenResponse, UserResponse

router = APIRouter()


@router.post("/init", response_model=BaseResponse[dict])
def init_admin(db: DB, data: UserInit) -> Any:
    user = db.query(AdminUser).first()
    if user:
        return BaseResponse(code=4001, message="Admin already initialized")

    new_user = AdminUser(username=data.username, password_hash=get_password_hash(data.password))
    db.add(new_user)
    db.commit()
    return BaseResponse(data={"initialized": True})


@router.post("/login", response_model=BaseResponse[TokenResponse])
def login(db: DB, data: UserLogin) -> Any:
    user = db.query(AdminUser).filter(AdminUser.username == data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        return BaseResponse(code=4011, message="Invalid username or password")

    user.last_login_at = datetime.now()
    db.commit()

    token = create_access_token(subject=str(user.id))
    return BaseResponse(
        data=TokenResponse(token=token, user=UserResponse(id=user.id, username=user.username))
    )


@router.get("/me", response_model=BaseResponse[UserResponse])
def get_me(current_user: CurrentUser) -> Any:
    return BaseResponse(data=UserResponse(id=current_user.id, username=current_user.username))


@router.post("/change-password", response_model=BaseResponse[dict])
def change_password(db: DB, current_user: CurrentUser, data: UserChangePassword) -> Any:
    if not verify_password(data.old_password, current_user.password_hash):
        return BaseResponse(code=4001, message="Incorrect old password")

    current_user.password_hash = get_password_hash(data.new_password)
    db.commit()
    return BaseResponse(data={})


@router.post("/logout", response_model=BaseResponse[dict])
def logout(current_user: CurrentUser) -> Any:
    return BaseResponse(data={})
