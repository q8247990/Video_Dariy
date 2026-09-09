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


#: 使用这些 fixture 的测试会被自动视为 PostgreSQL 集成测试（打上 postgres 标记），
#: 无需逐个写 ``@pytest.mark.postgres``。
PG_MARKED_FIXTURES = frozenset(
    {
        "postgres_engine",
        "postgres_migrated_engine",
        "postgres_session",
        "postgres_real_engine",
        "pg_engine",
        "pg_db",
        "pg_db_factory",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # 依赖 PG fixture 的测试自动打上 postgres 标记，保证 skip/选跑语义与显式标记一致。
    for item in items:
        if item.get_closest_marker("postgres"):
            continue
        if PG_MARKED_FIXTURES.intersection(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.postgres)

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
    """函数级 Session：外层事务在测试结束后回滚，保证行级隔离。

    Session 默认 ``join_transaction_mode="conditional_savepoint"`` 在
    连接已带活跃外层事务时退化为 ``rollback_only`` —— 测试代码的
    ``session.commit()`` 不真正提交（数据仍在将被 fixture 回滚的外层事务
    中），``session.rollback()`` 直接回滚外层事务，从而清空测试内所有写入。
    """

    connection = postgres_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# 全 PG 统一测试基建
#
# 项目统一用真实 PostgreSQL 做测试（不再用 SQLite）。以下 fixture 供
# tests/unit/ 与 tests/integration/ 的测试共用：
#
#   * ``pg_engine``         —— 已执行 alembic upgrade head 的一次性 schema engine
#   * ``pg_db``             —— 函数级 Session，外层事务回滚（替代旧 SQLite session）
#   * ``pg_db_factory``     —— 返回一个函数，调用一次得到一个独立 Session
#
# 所有使用这些 fixture 的测试会自动带上 postgres 标记（见
# ``pytest_collection_modifyitems`` 下方的标记逻辑），从而仅在
# ``pytest -m postgres`` 且设置 DATABASE_URL 时运行。
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_real_schema() -> str:
    """独立的一次性 schema，专供 ``pg_db_factory``（真提交可见）测试使用。

    与主 ``postgres_schema``（回滚隔离）物理隔离，避免真提交的数据
    污染 ``pg_db``/``pg_engine`` 测试。
    """

    return f"vd_real_{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session")
def postgres_real_engine(postgres_database_url: str, postgres_real_schema: str) -> Iterator[Engine]:
    """已跑过 alembic upgrade head 的一次性 schema engine，专供真提交测试。

    与 ``postgres_engine`` 复用相同创建/清理逻辑，但落在独立的
    ``postgres_real_schema`` 上，使 ``pg_db_factory`` 的 ``commit()``
    真正持久化到独立的 schema，跨连接可见且不污染回滚侧。
    """

    admin_engine = create_engine(postgres_database_url)
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{postgres_real_schema}"'))
        conn.commit()

    engine = create_engine(
        postgres_database_url,
        connect_args={"options": f"-csearch_path={postgres_real_schema}"},
    )
    run_alembic_upgrade_head(postgres_database_url, postgres_real_schema)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{postgres_real_schema}" CASCADE'))
            conn.commit()
        admin_engine.dispose()


@pytest.fixture(scope="session")
def pg_schema() -> str:
    """``pg_engine`` 专用的一次性 schema，与 ``postgres_schema`` 物理隔离。"""

    return f"vd_pg_{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session")
def pg_engine(postgres_database_url: str, pg_schema: str) -> Iterator[Engine]:
    """独立一次性 schema engine，已执行 alembic upgrade head。

    与 ``postgres_migrated_engine`` 物理隔离：直接通过
    ``postgres_migrated_engine`` 提交的数据无法污染 ``pg_db`` 测试。
    schema 生命周期与 ``postgres_real_engine`` 一致：建立 → alembic
    upgrade head → DROP SCHEMA CASCADE。
    """

    admin_engine = create_engine(postgres_database_url)
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{pg_schema}"'))
        conn.commit()

    engine = create_engine(
        postgres_database_url,
        connect_args={"options": f"-csearch_path={pg_schema}"},
    )
    run_alembic_upgrade_head(postgres_database_url, pg_schema)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{pg_schema}" CASCADE'))
            conn.commit()
        admin_engine.dispose()


@pytest.fixture()
def pg_db(pg_engine: Engine) -> Iterator[Session]:
    """函数级 PG Session：在独立迁移 schema 上，外层事务在 teardown 时回滚。

    Session 默认 ``join_transaction_mode="conditional_savepoint"`` 在
    连接已带活跃外层事务时退化为 ``rollback_only`` —— 测试代码的
    ``session.commit()`` 不真正提交（数据仍在将被 fixture 回滚的外层事务
    中），``session.rollback()`` 直接回滚外层事务，从而清空测试内所有写入，
    同测试外的 schema 完全不污染。
    """

    connection = pg_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture()
def pg_db_factory(postgres_real_engine: Engine):
    """返回一个工厂：调用一次得到一个独立、真实提交的 PG Session。

    与 ``pg_db``（外层事务回滚、数据对外不可见）不同，本工厂的 session
    不做外层事务包裹：``commit()`` 真正持久化，另一连接的 ``fresh``
    能立即看到。这服务于需要断言“提交后跨连接可见”的测试
    （如 outbox 事务性发布）。

    所有 session 都落在独立的 ``postgres_real_schema`` 上；fixture
    teardown 时将该 schema 内的业务表 TRUNCATE，清理真提交残留，
    既保证跨连接可见性，又避免污染下一次测试。
    """

    def _factory() -> Session:
        return Session(bind=postgres_real_engine)

    yield _factory

    _truncate_schema(postgres_real_engine)


def _truncate_schema(engine: Engine) -> None:
    """清空 schema 内所有业务表，清理真提交测试留下的残留数据。"""

    from src.db.base_class import Base  # local import 避免收集顺序问题

    table_names = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    if not table_names:
        return
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
        conn.commit()
