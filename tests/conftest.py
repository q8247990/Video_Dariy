"""pytest 全局基础设施：PostgreSQL 集成测试门禁与夹具。

PostgreSQL 集成测试使用 ``@pytest.mark.postgres`` 标记，并通过环境变量
``DATABASE_URL`` 显式指定目标数据库：

- 默认运行（未设置 ``DATABASE_URL`` 且未显式选择 ``postgres`` 标记）：
  postgres 测试会被**显式跳过**（skip reason 中说明原因），不影响 SQLite 单元测试。
- ``pytest -m postgres`` 但未设置 ``DATABASE_URL``：
  以**明确的 setup 失败**告终（不是静默跳过），防止 CI 误以为跑过 PG 测试。
- 每次运行使用独立的一次性 schema（``vd_test_<uuid>``），结束后 ``DROP SCHEMA ... CASCADE``，
  不在运行之间共享任何凭据或数据状态。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

DATABASE_URL_ENV = "DATABASE_URL"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

POSTGRES_MISSING_URL_MESSAGE = (
    "PostgreSQL 集成测试需要显式设置 DATABASE_URL 环境变量"
    "（例如 postgresql+psycopg://user:pass@127.0.0.1:5432/dbname）。"
    "测试会在该库中创建一次性 schema 并在结束时删除；不会使用任何默认凭据。"
)

POSTGRES_SKIP_REASON = f"未设置 {DATABASE_URL_ENV}，显式跳过 PostgreSQL 集成测试"


def _database_url() -> str | None:
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    return url or None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    postgres_items = [item for item in items if item.get_closest_marker("postgres")]
    if not postgres_items:
        return
    if _database_url():
        return
    markexpr = (config.option.markexpr or "").replace(" ", "")
    postgres_selected = "postgres" in markexpr and "notpostgres" not in markexpr
    if postgres_selected:
        # 用户显式要求跑 PG 测试但没给 DATABASE_URL：明确失败，而不是静默跳过。
        raise pytest.UsageError(POSTGRES_MISSING_URL_MESSAGE)
    skip_marker = pytest.mark.skip(reason=POSTGRES_SKIP_REASON)
    for item in postgres_items:
        item.add_marker(skip_marker)


def run_alembic_upgrade_head(database_url: str, schema: str) -> None:
    """在指定数据库的指定 schema 上执行 ``alembic upgrade head``。

    通过子进程运行以保证隔离；``PGOPTIONS`` 将 search_path 指向目标 schema，
    使 alembic_version 与所有业务表都落在该 schema 内。
    """

    env = dict(os.environ)
    env[DATABASE_URL_ENV] = database_url
    env["PGOPTIONS"] = f"-csearch_path={schema}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head 失败：\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


@pytest.fixture(scope="session")
def postgres_database_url() -> str:
    url = _database_url()
    if not url:
        pytest.fail(POSTGRES_MISSING_URL_MESSAGE, pytrace=False)
    return url


@pytest.fixture(scope="session")
def postgres_schema() -> str:
    """每次运行独立的一次性 schema 名。"""

    return f"vd_test_{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session")
def postgres_engine(postgres_database_url: str, postgres_schema: str) -> Iterator[Engine]:
    """会话级 PG engine：先建一次性 schema，结束后级联删除。"""

    admin_engine = create_engine(postgres_database_url)
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{postgres_schema}"'))
        conn.commit()

    engine = create_engine(
        postgres_database_url,
        connect_args={"options": f"-csearch_path={postgres_schema}"},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{postgres_schema}" CASCADE'))
            conn.commit()
        admin_engine.dispose()


@pytest.fixture(scope="session")
def postgres_migrated_engine(
    postgres_engine: Engine, postgres_database_url: str, postgres_schema: str
) -> Engine:
    """在一次性 schema 上执行过 ``alembic upgrade head`` 的 engine。"""

    run_alembic_upgrade_head(postgres_database_url, postgres_schema)
    return postgres_engine


@pytest.fixture()
def postgres_session(postgres_engine: Engine) -> Iterator[Session]:
    """函数级 Session：外层事务在测试结束后回滚，保证行级隔离。"""

    connection = postgres_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
