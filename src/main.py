# 以此项目纪念我亲爱的糖糖，愿你在喵星，也能看到家里，看到你的栗子哥哥，和永远爱你的爸爸妈妈。
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.api.v1.api import api_router
from src.core.celery_app import celery_app  # noqa: F401
from src.core.config import settings
from src.core.i18n import get_system_default_locale, normalize_locale
from src.db.init_db import get_current_alembic_revision, get_registered_table_names, init_db
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
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(LocaleMiddleware)

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
        status_code=200,
        content={"code": 4000, "message": str(exc), "data": None},
    )


app.include_router(api_router, prefix=settings.API_V1_STR)
app.include_router(mcp_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/health/bootstrap")
def health_bootstrap():
    return {
        "status": "ok",
        "schema_mode": "alembic_only",
        "alembic_revision": get_current_alembic_revision(),
        "registered_table_count": len(get_registered_table_names()),
        "registered_tables": get_registered_table_names(),
    }
