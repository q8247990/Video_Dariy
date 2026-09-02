# 以此项目纪念我亲爱的糖糖，愿你在喵星，也能看到家里，看到你的栗子哥哥，和永远爱你的爸爸妈妈。
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.api.error_status import ResponseStatusMiddleware
from src.api.v1.api import api_router
from src.core.celery_app import celery_app  # noqa: F401
from src.core.config import settings
from src.core.i18n import get_system_default_locale, normalize_locale
from src.db.init_db import get_current_alembic_revision, get_registered_table_names, init_db
from src.db.readiness import readiness_checks
from src.mcp.server import router as mcp_router


class LocaleMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        x_locale = (request.headers.get("X-Locale") or "").strip()
        accept_lang = (request.headers.get("Accept-Language") or "").strip()
        raw = x_locale or (accept_lang.split(",")[0].strip() if accept_lang else "")
        if raw:
            locale = normalize_locale(raw)
        else:
            locale = get_system_default_locale()
        request.state.locale = locale
        response = await call_next(request)
        response.headers["Content-Language"] = locale
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(LocaleMiddleware)
app.add_middleware(ResponseStatusMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"code": 4000, "message": str(exc), "data": None},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"code": 4000, "message": "Request validation failed", "data": None},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = 4011 if exc.status_code == status.HTTP_401_UNAUTHORIZED else exc.status_code
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": code, "message": str(exc.detail), "data": None},
        headers=exc.headers,
    )


app.include_router(api_router, prefix=settings.API_V1_STR)
app.include_router(mcp_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/livez")
def liveness_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readiness_check() -> JSONResponse:
    checks = readiness_checks()
    ready = all(checks.values())
    payload = {"status": "ready" if ready else "not_ready", "checks": checks}
    if ready:
        return JSONResponse(content=payload)
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)


@app.get("/health/bootstrap")
def health_bootstrap() -> dict[str, object]:
    return {
        "status": "ok",
        "schema_mode": "alembic_only",
        "alembic_revision": get_current_alembic_revision(),
        "registered_table_count": len(get_registered_table_names()),
        "registered_tables": get_registered_table_names(),
    }
