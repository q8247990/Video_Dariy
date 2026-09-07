"""Tests for task_db_session context manager (real PG behavior)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.session
from src.db.session import task_db_session
from src.models.video_source import VideoSource


@pytest.fixture()
def session_local_bound_to_test_engine(
    monkeypatch: pytest.MonkeyPatch,
    postgres_real_engine: Engine,
) -> sessionmaker[Session]:
    """Re-bind ``src.db.session.SessionLocal`` to the test's real-commit engine.

    This is the only acceptable form of test-time wiring for the
    production session factory: the real ``SessionLocal`` reads
    ``settings.SQLALCHEMY_DATABASE_URI`` and would point at a database
    unrelated to the test run. Pointing it at the
    ``postgres_real_engine`` lets every test in this module issue
    observable writes through the same connection pool that the
    second-connection assertions use to read them back.
    """

    factory = sessionmaker(bind=postgres_real_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(src.db.session, "SessionLocal", factory)
    return factory


def test_task_db_session_yields_usable_session(
    session_local_bound_to_test_engine: sessionmaker[Session],
) -> None:
    with task_db_session() as db:
        result = db.execute(text("SELECT 1 AS one")).scalar_one()
        assert result == 1


def test_task_db_session_closes_session_on_normal_exit(
    session_local_bound_to_test_engine: sessionmaker[Session],
    postgres_real_engine: Engine,
) -> None:
    baseline = postgres_real_engine.pool.checkedout()
    with task_db_session() as db:
        db.execute(text("SELECT 1"))
        assert postgres_real_engine.pool.checkedout() == baseline + 1

    assert postgres_real_engine.pool.checkedout() == baseline


def test_task_db_session_closes_session_on_exception(
    session_local_bound_to_test_engine: sessionmaker[Session],
    postgres_real_engine: Engine,
) -> None:
    baseline = postgres_real_engine.pool.checkedout()
    with pytest.raises(ValueError, match="simulated task failure"):
        with task_db_session() as db:
            db.execute(text("SELECT 1"))
            assert postgres_real_engine.pool.checkedout() == baseline + 1
            raise ValueError("simulated task failure")

    assert postgres_real_engine.pool.checkedout() == baseline


def test_task_db_session_does_not_auto_commit(
    session_local_bound_to_test_engine: sessionmaker[Session],
    pg_db_factory,
) -> None:
    with task_db_session() as db:
        db.add(
            VideoSource(
                source_name="uncommitted-row",
                camera_name="test",
                location_name="test",
                source_type="local_directory",
                enabled=True,
            )
        )
        db.flush()

    fresh = pg_db_factory()
    try:
        rows = fresh.query(VideoSource).filter(VideoSource.source_name == "uncommitted-row").all()
        assert rows == []
    finally:
        fresh.close()


def test_task_db_session_rolls_back_on_exception(
    session_local_bound_to_test_engine: sessionmaker[Session],
    pg_db_factory,
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with task_db_session() as db:
            db.add(
                VideoSource(
                    source_name="rolled-back-row",
                    camera_name="test",
                    location_name="test",
                    source_type="local_directory",
                    enabled=True,
                )
            )
            db.flush()
            raise RuntimeError("boom")

    fresh = pg_db_factory()
    try:
        rows = fresh.query(VideoSource).filter(VideoSource.source_name == "rolled-back-row").all()
        assert rows == []
    finally:
        fresh.close()


def test_task_db_session_propagates_exception(
    session_local_bound_to_test_engine: sessionmaker[Session],
) -> None:
    with pytest.raises(RuntimeError, match="should propagate"):
        with task_db_session():
            raise RuntimeError("should propagate")
