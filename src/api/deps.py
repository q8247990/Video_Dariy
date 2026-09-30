from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from src.application.bootstrap import Container, get_production_container
from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.core.config import settings
from src.core.i18n import DEFAULT_LOCALE
from src.core.security import ALGORITHM
from src.db.session import get_db
from src.models.admin_user import AdminUser
from src.schemas.auth import TokenData

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")

# Module-level reference to the process-wide production container
# (``src.application.bootstrap.get_production_container``). The API, MCP,
# and Celery entry points all share that single instance, so the process
# holds one dispatcher / LLM gateway factory / Celery control binding.
# Tests that need hermetic isolation override ``set_container_for_tests``
# from a fixture before any HTTP request fires.
_container: Container = get_production_container()


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
Locale = Annotated[str, Depends(get_locale)]


def get_container_dep() -> Container:
    """FastAPI dependency exposing the composition-root ``Container``.

    Endpoints that need direct access to a port (e.g. ``task_control``
    for revoke) depend on this instead of importing a concrete adapter.
    """

    return _container


ContainerDep = Annotated[Container, Depends(get_container_dep)]


def get_pipeline_orchestrator(
    container: ContainerDep,
) -> PipelineOrchestrator:
    """Build a :class:`PipelineOrchestrator` bound to the container's dispatcher."""

    return PipelineOrchestrator(dispatcher=container.dispatcher)


Orchestrator = Annotated[PipelineOrchestrator, Depends(get_pipeline_orchestrator)]


def set_container_for_tests(container: Container) -> None:
    """Replace the module-level container (test-only escape hatch).

    Production code never calls this — it exists so unit tests can
    substitute a fake-bound :class:`Container` without monkeypatching
    :mod:`src.application.bootstrap` or any concrete adapter.
    """

    global _container
    _container = container
