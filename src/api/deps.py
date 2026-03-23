from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.security import ALGORITHM
from src.db.session import get_db
from src.models.admin_user import AdminUser
from src.schemas.auth import TokenData

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")


def get_current_user(
    db: Annotated[Session, Depends(get_db)],
    token: Annotated[str, Depends(oauth2_scheme)],
) -> AdminUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str = payload.get("sub")
        if user_id_str is None:
            raise credentials_exception
        token_data = TokenData(sub=str(user_id_str))
    except JWTError as e:
        raise credentials_exception from e

    if token_data.sub is None:
        raise credentials_exception

    user = db.query(AdminUser).filter(AdminUser.id == int(token_data.sub)).first()
    if user is None:
        raise credentials_exception
    return user


CurrentUser = Annotated[AdminUser, Depends(get_current_user)]
DB = Annotated[Session, Depends(get_db)]
