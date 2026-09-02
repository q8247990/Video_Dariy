"""PostgreSQL integration test for the backup -> verify -> restore round-trip.

Proves that :mod:`scripts.backup_restore_db` can dump a migrated schema,
checksum it, and restore it into a THROWAWAY temp database such that the
Alembic revision and key row counts match the backup source. This is the
runbook guarantee operators rely on before an irreversible migration
(``20260825_0012`` / ``20260825_0014`` / ``20260826_0017`` in AGENTS.md).

The source is the conftest one-time migrated schema; the target is a fresh
throwaway database on the same PG server, dropped in the teardown.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from scripts.backup_restore_db import run_backup, run_restore, verify_checksum
from src.models.task_log import TaskLog
from src.models.video_source import VideoSource

pytestmark = pytest.mark.postgres


@pytest.fixture()
def temp_backup_db(postgres_database_url: str) -> str:
    """Create a throwaway database name (created lazily, dropped on teardown)."""
    url = make_url(postgres_database_url)
    admin_url = url.set(drivername="postgresql+psycopg", database="postgres")
    dbname = f"vd_bk_{uuid.uuid4().hex[:12]}"
    # Build the target URL from real components: ``str(URL)`` masks the
    # password to ``***``, which would break pg tools' PGPASSWORD.
    target_url = f"{url.drivername}://{url.username}:{url.password}@{url.host}:{url.port}/{dbname}"
    _psql(admin_url, f'CREATE DATABASE "{dbname}"')
    try:
        yield target_url
    finally:
        _psql(admin_url, f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


def _psql(database_url: str, statement: str) -> None:
    url = make_url(database_url)
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    cmd = ["psql", "-h", str(url.host), "-p", str(url.port), "-U", str(url.username)]
    cmd += ["-d", str(url.database), "-v", "ON_ERROR_STOP=1", "-c", statement]
    subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)


def _seed_source_rows(engine: Engine) -> dict[str, int]:
    """Insert known rows into the source schema; return per-table counts."""
    with Session(engine) as session:
        for i in range(3):
            session.add(
                VideoSource(
                    source_name=f"bk-source-{i}",
                    camera_name="cam",
                    location_name="home",
                    source_type="local_directory",
                    config_json={"root_path": "/tmp"},
                    enabled=True,
                )
            )
        for _ in range(2):
            session.add(
                TaskLog(
                    task_type="session_build",
                    status="success",
                    retry_count=0,
                    recovery_attempt=0,
                    cancel_requested=False,
                )
            )
        session.commit()
    return {"video_source": 3, "task_log": 2}


def _counts_in(engine: Engine, tables: list[str]) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            t: int(conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one()) for t in tables
        }


def test_backup_verify_restore_roundtrip_preserves_revision_and_counts(
    postgres_database_url: str,
    postgres_schema: str,
    postgres_migrated_engine: Engine,
    temp_backup_db: str,
    tmp_path: Path,
) -> None:
    seeded = _seed_source_rows(postgres_migrated_engine)

    with postgres_migrated_engine.connect() as conn:
        source_revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    source_counts = _counts_in(postgres_migrated_engine, ["video_source", "task_log"])

    dump = tmp_path / "source.dump"
    run_backup(postgres_database_url, dump, schema=postgres_schema)
    assert verify_checksum(dump) is True

    _psql(temp_backup_db, f'CREATE SCHEMA "{postgres_schema}"')
    run_restore(temp_backup_db, dump, schema=postgres_schema)

    target_engine = create_engine(
        temp_backup_db, connect_args={"options": f"-csearch_path={postgres_schema}"}
    )
    try:
        with target_engine.connect() as conn:
            restored_revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            restored_counts = _counts_in(target_engine, ["video_source", "task_log"])
    finally:
        target_engine.dispose()

    assert restored_revision == source_revision == "20260902_0021"
    assert restored_counts == source_counts == seeded


def test_verify_detects_corrupted_dump(postgres_database_url: str, tmp_path: Path) -> None:
    dump = tmp_path / "corrupt.dump"
    dump.write_bytes(b"not a real postgres dump")
    dump.with_suffix(".dump.sha256").write_text("deadbeef  corrupt.dump\n", encoding="utf-8")
    assert verify_checksum(dump) is False
