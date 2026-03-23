# 以此项目纪念我亲爱的糖糖，愿你在喵星，也能看到家里，看到你的栗子哥哥，和永远爱你的爸爸妈妈。
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.v1.api import api_router
from src.core.celery_app import celery_app  # noqa: F401
from src.core.config import settings
from src.db.init_db import get_current_alembic_revision, get_registered_table_names, init_db
from src.mcp.server import router as mcp_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# Set all CORS enabled origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
