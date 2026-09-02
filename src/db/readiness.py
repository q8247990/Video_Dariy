import logging

import redis as redis_lib
from sqlalchemy import text

from src.core.config import settings
from src.db.init_db import EXPECTED_ALEMBIC_REVISION, get_current_alembic_revision
from src.db.session import engine

logger = logging.getLogger(__name__)


def check_database() -> bool:
    """Return True when a trivial ``SELECT 1`` against the app engine succeeds."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.debug("DB readiness check failed", exc_info=True)
        return False


def check_redis() -> bool:
    """Return True when the configured Redis broker answers PING."""
    try:
        client = redis_lib.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        logger.debug("Redis readiness check failed", exc_info=True)
        return False


def check_alembic_head() -> bool:
    """Return True when the live schema is at the expected Alembic head."""
    return get_current_alembic_revision() == EXPECTED_ALEMBIC_REVISION


def readiness_checks() -> dict[str, bool]:
    """Return a name -> healthy map for every readiness dependency."""
    return {
        "database": check_database(),
        "redis": check_redis(),
        "alembic_head": check_alembic_head(),
    }
