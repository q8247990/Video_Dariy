"""恢复整改前（pre-remediation）PostgreSQL 种子快照并校验行数。

该脚本是**显式工具**，不接入默认 pytest 运行。典型用途：TODO 6 时区迁移前，
在一次性数据库/schema 上重建整改前数据基线，验证迁移行数与时间锚点。

用法::

    # 目标库已执行过 alembic upgrade head（库内已有业务表）
    DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/dbname \\
        python3 scripts/restore_pre_remediation_seed.py

    # 让脚本先自动执行 alembic upgrade head（建表），再灌入快照
    DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/dbname \\
        python3 scripts/restore_pre_remediation_seed.py --with-alembic --schema my_seed_schema

    # 灌入后保留 schema 便于手工检查（默认结束时会 DROP SCHEMA）
    ... --keep

行为约定：
- 默认在随机一次性 schema（vd_seed_<uuid>）中操作，结束时 DROP SCHEMA CASCADE，
  不共享/污染任何既有数据；
- 灌入后调用 verify_pre_remediation 校验 EXPECTED_ROW_COUNTS，不一致则退出码为 1；
- 所有时间戳为 naive datetime，按 Asia/Shanghai 墙钟解释（见种子模块 docstring）。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from tests.conftest import run_alembic_upgrade_head  # noqa: E402
from tests.fixtures.pre_remediation_seed import (  # noqa: E402
    EXPECTED_ROW_COUNTS,
    SEED_HOME_TIMEZONE,
    SEED_VERSION,
    seed_pre_remediation,
    verify_pre_remediation,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复整改前 PG 种子快照并校验行数")
    parser.add_argument(
        "--with-alembic",
        action="store_true",
        help="灌入前先执行 alembic upgrade head（在目标 schema 内建表）",
    )
    parser.add_argument("--schema", default=None, help="目标 schema（默认随机 vd_seed_<uuid>）")
    parser.add_argument("--keep", action="store_true", help="结束后保留 schema（默认删除）")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: 必须显式设置 DATABASE_URL 环境变量", file=sys.stderr)
        return 2

    schema = args.schema or f"vd_seed_{uuid.uuid4().hex[:12]}"
    print(f"[seed] version={SEED_VERSION} home_timezone={SEED_HOME_TIMEZONE} schema={schema}")

    admin_engine = create_engine(database_url)
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.commit()

    if args.with_alembic:
        print("[seed] running alembic upgrade head ...")
        run_alembic_upgrade_head(database_url, schema)

    engine = create_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}
    )
    try:
        with engine.connect() as conn:
            with Session(bind=conn) as session:
                seed_pre_remediation(session)
                session.commit()
            with Session(bind=conn) as session:
                actual = verify_pre_remediation(session)
        print("[seed] row counts verified:")
        for table, count in actual.items():
            print(f"  {table}: {count} (expected {EXPECTED_ROW_COUNTS[table]})")
        print("[seed] OK")
        return 0
    finally:
        engine.dispose()
        if not args.keep:
            with admin_engine.connect() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                conn.commit()
            print(f"[seed] schema {schema} dropped")
        admin_engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
