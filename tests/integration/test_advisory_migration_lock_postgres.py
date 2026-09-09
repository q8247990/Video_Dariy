"""PostgreSQL integration tests for the migration advisory lock.

``src.db.init_db.run_migration_locked`` wraps the Alembic upgrade in a named
PostgreSQL advisory lock so two backend containers starting simultaneously
never migrate concurrently. These tests prove the serialization semantics
against a fresh throwaway schema:

* two concurrent bootstrap attempts -> exactly one runs the migration, the
  other blocks then finds the schema already at head;
* a held lock is observable (``pg_try_advisory_lock`` returns ``False``) and
  released afterwards.

The migrator body invokes the canonical
``tests.conftest.run_alembic_upgrade_head`` subprocess helper — the same
seam :mod:`tests.integration.test_llm_provider_drop_columns_postgres` uses
— so the migration runs through the public alembic CLI surface, not the
private in-process ``_run_alembic_upgrade_head``. The lock-semantics
assertion (concurrent bootstrap serializes; migration runs once) is
preserved.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from src.db.init_db import (
    EXPECTED_ALEMBIC_REVISION,
    MIGRATION_ADVISORY_LOCK_KEY,
    run_migration_locked,
)
from tests.conftest import run_alembic_upgrade_head

pytestmark = pytest.mark.postgres


def _make_schema_engine(postgres_database_url: str) -> tuple[str, Engine]:
    schema = f"vd_locktest_{uuid.uuid4().hex[:10]}"
    admin = create_engine(postgres_database_url)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.commit()
    finally:
        admin.dispose()
    engine = create_engine(
        postgres_database_url, connect_args={"options": f"-csearch_path={schema}"}
    )
    return schema, engine


def _drop_schema_engine(postgres_database_url: str, schema: str, engine: Engine) -> None:
    admin = create_engine(postgres_database_url)
    engine.dispose()
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.commit()
    finally:
        admin.dispose()


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        present = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'alembic_version' "
                "AND table_schema = current_schema()"
            )
        ).first()
        if present is None:
            return None
        row = conn.execute(
            text("SELECT version_num FROM alembic_version ORDER BY version_num LIMIT 1")
        ).first()
    return str(row[0]) if row else None


def _bootstrap_attempt(
    engine: Engine,
    schema: str,
    postgres_database_url: str,
    migrator_tags: list[bool],
    barrier: threading.Barrier,
) -> None:
    def migrate() -> None:
        if _current_revision(engine) != EXPECTED_ALEMBIC_REVISION:
            migrator_tags.append(True)
            run_alembic_upgrade_head(postgres_database_url, schema)

    barrier.wait()
    run_migration_locked(migrate, lock_engine=engine)


def test_concurrent_bootstrap_serializes_and_migrates_once(
    postgres_database_url: str,
) -> None:
    schema, engine = _make_schema_engine(postgres_database_url)
    migrator_tags: list[bool] = []
    barrier = threading.Barrier(2)

    threads = [
        threading.Thread(
            target=_bootstrap_attempt,
            args=(engine, schema, postgres_database_url, migrator_tags, barrier),
        )
        for _ in range(2)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)
        for t in threads:
            assert not t.is_alive(), "bootstrap thread did not finish"

        assert len(migrator_tags) == 1, "exactly one attempt should own the migration"
        assert _current_revision(engine) == EXPECTED_ALEMBIC_REVISION
        table_names = set(inspect(engine).get_table_names())
        for expected in ("video_source", "video_session", "event_record", "task_log"):
            assert expected in table_names
    finally:
        _drop_schema_engine(postgres_database_url, schema, engine)


def test_lock_is_held_then_released(postgres_database_url: str) -> None:
    schema, engine = _make_schema_engine(postgres_database_url)
    results: list[bool] = []
    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with engine.connect() as conn:
            conn.execute(text(f"SELECT pg_advisory_lock({MIGRATION_ADVISORY_LOCK_KEY})"))
            try:
                acquired.set()
                release.wait(timeout=30)
            finally:
                conn.execute(text(f"SELECT pg_advisory_unlock({MIGRATION_ADVISORY_LOCK_KEY})"))

    def try_acquire() -> None:
        if not acquired.wait(timeout=30):
            return
        with engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT pg_try_advisory_lock({MIGRATION_ADVISORY_LOCK_KEY})")
            ).scalar()
            results.append(bool(row))
            conn.execute(text(f"SELECT pg_advisory_unlock({MIGRATION_ADVISORY_LOCK_KEY})"))

    try:
        t_holder = threading.Thread(target=holder)
        t_try = threading.Thread(target=try_acquire)
        t_holder.start()
        t_try.start()
        t_try.join(timeout=30)
        assert acquired.is_set(), "holder should signal once it owns the lock"
        release.set()
        t_holder.join(timeout=30)
        assert results == [False], "lock should be held by the holder while it runs"

        with engine.connect() as conn:
            reacquired = conn.execute(
                text(f"SELECT pg_try_advisory_lock({MIGRATION_ADVISORY_LOCK_KEY})")
            ).scalar()
            conn.execute(text(f"SELECT pg_advisory_unlock({MIGRATION_ADVISORY_LOCK_KEY})"))
        assert bool(reacquired) is True, "lock should be releasable once the holder exits"
    finally:
        _drop_schema_engine(postgres_database_url, schema, engine)
