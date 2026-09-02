"""PostgreSQL integration tests for the LLMProvider keyframe column-drop.

These tests pin the contract established by tod 10 / migration
``20260902_0018``:

* empty databases upgrade cleanly to the new head and the three retired
  columns are absent from ``information_schema.columns``;
* pre-existing databases (i.e. databases whose schema matches the
  pre-migration head ``20260826_0017``) can carry legacy data through
  those columns; the migration drops the columns and the surviving
  rows remain readable.

Both runs share the same SQLAlchemy fixtures as the rest of the
``@pytest.mark.postgres`` suite (``postgres_database_url`` /
``postgres_engine``). The seeded-database test creates its own
schema so the upgrade can be staged (upgrade to the previous head,
insert legacy row, upgrade to the new head) without rolling back the
already-applied ``20260902_0018`` on the shared schema (the migration
is irreversible).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests.conftest import PROJECT_ROOT

pytestmark = pytest.mark.postgres

DROPPED_COLUMNS: tuple[str, ...] = (
    "video_preprocess_mode",
    "video_keyframe_target_n",
    "video_keyframe_jpeg_quality",
)


def _alembic(database_url: str, schema: str, *args: str) -> None:
    """Run ``alembic`` in a subprocess targeting a specific schema.

    Mirrors ``tests.conftest.run_alembic_upgrade_head`` but accepts an
    arbitrary alembic command so tests can stage to a non-head revision
    before exercising the contract migration.
    """

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["PGOPTIONS"] = f"-csearch_path={schema}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} failed:\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


def _llm_provider_columns(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'llm_provider'"
            )
        ).fetchall()
    }


def _isolated_schema(database_url: str) -> Iterator[tuple[Engine, str]]:
    """Yield (engine, schema) for a freshly-created disposable schema.

    Function-scoped: each call returns its own schema, so staging
    ``upgrade 20260826_0017`` then ``upgrade head`` does not collide
    with the session-scoped ``postgres_migrated_engine`` schema that
    already sits on the new head.
    """

    schema = f"vd_test_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(database_url)
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.commit()
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        yield engine, schema
    finally:
        engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.commit()
        admin_engine.dispose()


def test_empty_database_upgrade_drops_three_columns(
    postgres_migrated_engine: Engine,
) -> None:
    """A clean ``alembic upgrade head`` lands on the new head; the
    retired columns must not appear in ``information_schema.columns``.
    """

    with postgres_migrated_engine.connect() as conn:
        columns = _llm_provider_columns(conn)
        for dropped in DROPPED_COLUMNS:
            assert dropped not in columns, (
                f"column {dropped!r} should have been dropped by 20260902_0018 but is still present"
            )


def test_seeded_database_upgrade_drops_three_columns_and_keeps_rows(
    postgres_database_url: str,
) -> None:
    """Simulate a production database that sits on the pre-tod-10 head
    (``20260826_0017``) with a real provider row that uses the three
    retired columns. ``alembic upgrade head`` must drop the columns and
    preserve the row data; the upgrade is the only irreversible step in
    this flow, so this test also implicitly documents the
    backup-before-upgrade requirement.
    """

    gen = _isolated_schema(postgres_database_url)
    engine, schema = next(gen)
    try:
        # 1. Stage the database to the previous head.
        _alembic(postgres_database_url, schema, "upgrade", "20260826_0017")

        with engine.begin() as conn:
            pre_columns = _llm_provider_columns(conn)
            for dropped in DROPPED_COLUMNS:
                assert dropped in pre_columns, (
                    f"fixture invariant violated: {dropped!r} should exist "
                    "on the pre-migration head"
                )

            conn.execute(
                text(
                    "INSERT INTO llm_provider ("
                    "provider_name, provider_type, api_base_url, api_key, "
                    "model_name, timeout_seconds, retry_count, enabled, "
                    "supports_vision, supports_qa, supports_tool_calling, "
                    "is_default_vision, is_default_qa, "
                    "video_preprocess_mode, video_keyframe_target_n, "
                    "video_keyframe_jpeg_quality, created_at, updated_at"
                    ") VALUES ("
                    ":provider_name, :provider_type, :api_base_url, :api_key, "
                    ":model_name, :timeout_seconds, :retry_count, :enabled, "
                    ":supports_vision, :supports_qa, :supports_tool_calling, "
                    ":is_default_vision, :is_default_qa, "
                    ":video_preprocess_mode, :video_keyframe_target_n, "
                    ":video_keyframe_jpeg_quality, :created_at, :updated_at"
                    ")"
                ),
                {
                    "provider_name": "legacy-keyframe-provider",
                    "provider_type": "qa_provider",
                    "api_base_url": "http://example.com/v1",
                    "api_key": "v1:placeholder-encrypted-blob",
                    "model_name": "qwen3.5-9b",
                    "timeout_seconds": 60,
                    "retry_count": 3,
                    "enabled": True,
                    "supports_vision": False,
                    "supports_qa": True,
                    "supports_tool_calling": False,
                    "is_default_vision": False,
                    "is_default_qa": False,
                    "video_preprocess_mode": "raw_mp4",
                    "video_keyframe_target_n": 120,
                    "video_keyframe_jpeg_quality": 88,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )

            pre_count = conn.execute(text("SELECT count(*) FROM llm_provider")).scalar_one()
            assert pre_count == 1

        # 2. Apply the contract migration.
        _alembic(postgres_database_url, schema, "upgrade", "head")

        # 3. Verify the columns are gone and the row survives intact.
        with engine.connect() as conn:
            post_columns = _llm_provider_columns(conn)
            for dropped in DROPPED_COLUMNS:
                assert dropped not in post_columns, (
                    f"column {dropped!r} should have been dropped by "
                    "20260902_0018 but is still present after upgrade"
                )

            post_count = conn.execute(text("SELECT count(*) FROM llm_provider")).scalar_one()
            assert post_count == 1, (
                "row data must survive the column drop; restoration is "
                "only possible from the verified pre-upgrade backup"
            )

            surviving_name = conn.execute(
                text("SELECT provider_name FROM llm_provider")
            ).scalar_one()
            assert surviving_name == "legacy-keyframe-provider"
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_migration_preflight_refuses_when_columns_already_absent(
    postgres_database_url: str,
) -> None:
    """The migration's preflight must abort (RuntimeError) when any of
    the three columns is already absent on ``llm_provider``. The check
    guards against silently no-op'ing on a half-applied or
    out-of-band-cleaned database.
    """

    gen = _isolated_schema(postgres_database_url)
    engine, schema = next(gen)
    try:
        # Stage to the previous head.
        _alembic(postgres_database_url, schema, "upgrade", "20260826_0017")

        # Drop one of the three columns out-of-band to simulate a
        # half-applied migration; the next run of 20260902_0018 must
        # refuse to proceed instead of silently no-op'ing.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE llm_provider DROP COLUMN video_preprocess_mode"))

        with pytest.raises(RuntimeError) as exc_info:
            _alembic(postgres_database_url, schema, "upgrade", "20260902_0018")

        assert "video_preprocess_mode" in str(exc_info.value)
        assert "preflight failed" in str(exc_info.value).lower()
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_migration_downgrade_is_irreversible(postgres_database_url: str) -> None:
    """``alembic downgrade`` against ``20260902_0018`` must raise
    ``NotImplementedError``; the operator must restore from a verified
    backup to roll back. Locked at the contract level.
    """

    gen = _isolated_schema(postgres_database_url)
    _engine, schema = next(gen)
    try:
        _alembic(postgres_database_url, schema, "upgrade", "20260826_0017")
        _alembic(postgres_database_url, schema, "upgrade", "20260902_0018")

        env = dict(os.environ)
        env["DATABASE_URL"] = postgres_database_url
        env["PGOPTIONS"] = f"-csearch_path={schema}"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "downgrade",
                "-1",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode != 0, (
            "downgrade must fail: migration 20260902_0018 is irreversible"
        )
        combined = result.stdout + result.stderr
        assert "NotImplementedError" in combined
        assert "restore from verified DB backup" in combined
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
