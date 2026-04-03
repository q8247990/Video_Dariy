from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.config import settings

engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URI,
    pool_pre_ping=True,
    pool_recycle=3600,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def task_db_session():
    """Context manager for Celery task DB sessions.

    Handles session creation and guaranteed cleanup.
    Commit/rollback is the caller's responsibility.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
