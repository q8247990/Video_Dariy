from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from src.application.bootstrap import Container
from src.application.container_holder import get_container, set_container_for_tests
from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.core.config import settings
from src.core.i18n import DEFAULT_LOCALE
from src.core.security import ALGORITHM
from src.db.session import get_db
from src.models.admin_user import AdminUser
from src.schemas.auth import TokenData

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")


def get_locale(request: Request) -> str:
    return getattr(request.state, "locale", DEFAULT_LOCALE)


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
        user_id = int(str(token_data.sub))
    except (JWTError, TypeError, ValueError) as e:
        raise credentials_exception from e

    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user


CurrentUser = Annotated[AdminUser, Depends(get_current_user)]
DB = Annotated[Session, Depends(get_db)]
Locale = Annotated[str, Depends(get_locale)]


def get_container_dep() -> Container:
    return get_container()


ContainerDep = Annotated[Container, Depends(get_container_dep)]


def get_pipeline_orchestrator(
    container: ContainerDep,
) -> PipelineOrchestrator:
    """Build a :class:`PipelineOrchestrator` bound to the container's dispatcher."""

    return PipelineOrchestrator(dispatcher=container.dispatcher)


Orchestrator = Annotated[PipelineOrchestrator, Depends(get_pipeline_orchestrator)]

__all__ = [
    "ContainerDep",
    "CurrentUser",
    "DB",
    "Locale",
    "Orchestrator",
    "get_container_dep",
    "get_current_user",
    "get_locale",
    "get_pipeline_orchestrator",
    "oauth2_scheme",
    "set_container_for_tests",
]
