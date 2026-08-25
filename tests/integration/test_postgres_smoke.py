"""PostgreSQL 集成冒烟测试：验证 conftest 的 PG 夹具与整改前种子快照。

运行方式::

    DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db pytest -m postgres

未设置 DATABASE_URL 时，显式选择 ``-m postgres`` 会以明确的 setup 失败结束；
默认运行中这些测试会被显式跳过（见 tests/conftest.py）。
"""

from datetime import timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tests.fixtures.pre_remediation_seed import (
    ANCHOR_SESSION_START,
    EXPECTED_ROW_COUNTS,
    SEED_VERSION,
    seed_pre_remediation,
    verify_pre_remediation,
)

pytestmark = pytest.mark.postgres


def test_postgres_engine_uses_isolated_schema(
    postgres_engine: Engine, postgres_schema: str
) -> None:
    with postgres_engine.connect() as conn:
        current_schema = conn.execute(text("SELECT current_schema()")).scalar_one()
        assert current_schema == postgres_schema
        assert postgres_schema.startswith("vd_test_")


def test_alembic_upgrade_head_on_throwaway_schema(postgres_migrated_engine: Engine) -> None:
    with postgres_migrated_engine.connect() as conn:
        version_rows = conn.execute(text("SELECT version_num FROM alembic_version")).all()
        assert len(version_rows) == 1
        table_names = set(inspect(conn).get_table_names())
        expected_tables = (
            "video_source",
            "video_session",
            "event_record",
            "task_log",
            "daily_summary",
        )
        for expected in expected_tables:
            assert expected in table_names


def test_pre_remediation_seed_restore_and_verify(
    postgres_migrated_engine: Engine, postgres_schema: str
) -> None:
    with postgres_migrated_engine.connect() as conn:
        with Session(bind=conn) as session:
            existing_session_count = session.execute(
                text("SELECT count(*) FROM video_session")
            ).scalar_one()
            session.execute(text("TRUNCATE TABLE video_source, task_log, daily_summary CASCADE"))
            seed_pre_remediation(session)
            actual = verify_pre_remediation(session)
            assert actual == EXPECTED_ROW_COUNTS

            # 0014 迁移后为 timestamptz；naive 锚点按 UTC 解释并回读为 aware-UTC。
            row = conn.execute(
                text("SELECT session_start_time FROM video_session WHERE id = 1")
            ).scalar_one()
            assert row == ANCHOR_SESSION_START.replace(tzinfo=timezone.utc)
            assert row.tzinfo is not None

            session.rollback()
        # 显式回滚后库内无残留，保证测试之间无共享状态
        with Session(bind=conn) as check_session:
            remaining = check_session.execute(
                text("SELECT count(*) FROM video_session")
            ).scalar_one()
            assert remaining == existing_session_count


def test_seed_version_pinned() -> None:
    # 快照版本号是迁移测试的比对基准，禁止无声变更
    assert SEED_VERSION == "pre-remediation.v1"
