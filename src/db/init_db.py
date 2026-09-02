import logging
import time
from collections.abc import Callable
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from alembic import command
from src.core.security import get_password_hash
from src.db.session import SessionLocal, engine
from src.models.admin_user import AdminUser

logger = logging.getLogger(__name__)

#: The Alembic revision this deployment expects to be at. Readiness gates
#: (``/readyz``) compare the live schema against this head.
EXPECTED_ALEMBIC_REVISION = "20260902_0021"

#: Named PostgreSQL advisory-lock key used to serialize schema migration across
#: concurrently starting backend containers. Advisory locks are scoped to a
#: database, so every backend pointing at the same DB coordinates on this key.
MIGRATION_ADVISORY_LOCK_KEY = 6_420_302_021


def get_current_alembic_revision() -> str | None:
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            ).first()
            if row is None:
                return None
            return str(row[0]) if row[0] is not None else None
    except SQLAlchemyError:
        logger.debug("Could not read current Alembic revision", exc_info=True)
        return None


def get_registered_table_names() -> list[str]:
    from src.db.base import Base

    return sorted(Base.metadata.tables.keys())


def _run_alembic_upgrade_head() -> None:
    project_root = Path(__file__).resolve().parents[2]
    alembic_ini_path = project_root / "alembic.ini"
    if not alembic_ini_path.exists():
        raise RuntimeError("alembic.ini not found, cannot run schema upgrade")

    config = Config(str(alembic_ini_path))
    command.upgrade(config, "head")


def run_migration_locked(migrator: Callable[[], None], lock_engine: Engine | None = None) -> None:
    """Run ``migrator()`` guarded by a named PostgreSQL advisory lock.

    Two backend containers starting simultaneously must never migrate
    concurrently: exactly one owns the migration while the other waits on
    the lock, then finds the schema already at head. Advisory locks are
    session-scoped and held on a dedicated connection until ``migrator()``
    returns. Non-PostgreSQL dialects (e.g. SQLite unit tests) skip the lock.
    """

    target = lock_engine if lock_engine is not None else engine
    if target.dialect.name != "postgresql":
        migrator()
        return

    with target.connect() as lock_conn:
        lock_conn.execute(text(f"SELECT pg_advisory_lock({MIGRATION_ADVISORY_LOCK_KEY})"))
        try:
            migrator()
        finally:
            lock_conn.execute(text(f"SELECT pg_advisory_unlock({MIGRATION_ADVISORY_LOCK_KEY})"))


def _is_retryable_operational_error(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    pgcode = str(getattr(original, "pgcode", "") or "")
    if pgcode in {"57P03", "08001", "08006"}:
        return True

    message = str(exc).lower()
    retryable_markers = [
        "connection refused",
        "could not connect",
        "the database system is starting up",
        "temporary failure in name resolution",
    ]
    return any(marker in message for marker in retryable_markers)


def _ensure_default_admin() -> None:
    from src.core.config import settings

    db: Session = SessionLocal()
    try:
        admin = db.query(AdminUser).first()
        if admin:
            logger.info("Admin user already exists, skipping default admin creation")
            return

        default_admin = AdminUser(
            username=settings.DEFAULT_ADMIN_USERNAME,
            password_hash=get_password_hash(settings.DEFAULT_ADMIN_PASSWORD),
        )
        db.add(default_admin)
        db.commit()
        logger.info("Default admin created: username=%s", settings.DEFAULT_ADMIN_USERNAME)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db(max_retries: int | None = None, retry_interval: int | None = None) -> None:
    """Initialize PostgreSQL schema and seed default admin."""
    from src.core.config import settings

    effective_max_retries = max_retries if max_retries is not None else settings.DB_INIT_MAX_RETRIES
    effective_retry_interval = (
        retry_interval if retry_interval is not None else settings.DB_INIT_RETRY_INTERVAL_SECONDS
    )

    for attempt in range(1, effective_max_retries + 1):
        try:
            run_migration_locked(_run_alembic_upgrade_head)
            _ensure_default_admin()
            logger.info(
                "Database bootstrap done (mode=%s, tables=%s, alembic_revision=%s)",
                "alembic_upgrade",
                len(get_registered_table_names()),
                get_current_alembic_revision() or "none",
            )
            return
        except OperationalError as exc:
            if not _is_retryable_operational_error(exc):
                logger.exception("Database initialization failed due to non-retryable SQL error")
                raise

            if attempt == effective_max_retries:
                logger.exception("Database initialization failed after retries")
                raise

            logger.warning(
                "Database is not ready, retrying (%s/%s)",
                attempt,
                effective_max_retries,
            )
            time.sleep(effective_retry_interval)
