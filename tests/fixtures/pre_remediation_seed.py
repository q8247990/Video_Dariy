"""整改前（pre-remediation）PostgreSQL 种子快照夹具。

用途：为后续迁移类任务（TODO 6 时间戳 UTC 化迁移）提供一份**版本化、确定性**的
整改前数据快照，包含 naive（无时区）时间戳的 source/file/session/event/task/daily
代表性行。所有 naive datetime 一律按 `Asia/Shanghai` 墙钟时间解释（见整改计划）。

版本：`SEED_VERSION` 变动即代表快照内容变化，迁移测试应记录所依据的版本。

使用方式：
- 不接入默认 pytest 运行；由 ``scripts/restore_pre_remediation_seed.py`` 或
  带 ``@pytest.mark.postgres`` 的迁移测试显式调用。
- 目标库需已执行 ``alembic upgrade head``（可用
  ``tests.conftest.run_alembic_upgrade_head`` 或脚本 ``--with-alembic``）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource

SEED_VERSION = "pre-remediation.v1"

# 快照中所有 naive datetime 的墙钟时区约定（整改计划：历史 naive 值按 Asia/Shanghai 解释）
SEED_HOME_TIMEZONE = "Asia/Shanghai"

SOURCE_ID = 1
SESSION_ID = 1

EXPECTED_ROW_COUNTS: dict[str, int] = {
    "video_source": 1,
    "video_file": 2,
    "video_session": 1,
    "video_session_file_rel": 2,
    "event_record": 2,
    "task_log": 1,
    "daily_summary": 1,
}

# 关键墙钟时间锚点（Asia/Shanghai naive），供迁移后比对 UTC 瞬时值
ANCHOR_SESSION_START = datetime(2026, 3, 14, 8, 0, 0)
ANCHOR_SESSION_END = datetime(2026, 3, 14, 8, 2, 0)
ANCHOR_EVENT_MORNING = datetime(2026, 3, 14, 8, 0, 30)
ANCHOR_EVENT_CROSS_DAY = datetime(2026, 3, 14, 23, 55, 0)  # 接近日界，验证 local day 归属
ANCHOR_TASK_STARTED = datetime(2026, 3, 14, 8, 3, 0)
ANCHOR_SUMMARY_GENERATED_AT = datetime(2026, 3, 15, 7, 30, 0)
ANCHOR_SUMMARY_DATE = date(2026, 3, 14)


def seed_pre_remediation(session: Session) -> dict[str, int]:
    """向给定 session 写入整改前快照，返回实际写入的行数。"""

    source = VideoSource(
        id=SOURCE_ID,
        source_name="seed-camera-living-room",
        camera_name="living_room_cam",
        location_name="客厅",
        source_type="local_directory",
        config_json={"path": "/seed/videos/living_room"},
        enabled=True,
    )
    session.add(source)

    file_a = VideoFile(
        id=1,
        source_id=SOURCE_ID,
        file_name="0800.mp4",
        file_path="/seed/videos/living_room/0800.mp4",
        file_path_hash="seed_hash_0800",
        storage_type="local_file",
        file_format="mp4",
        start_time=ANCHOR_SESSION_START,
        end_time=datetime(2026, 3, 14, 8, 1, 0),
        duration_seconds=60,
        parse_status="parsed",
    )
    file_b = VideoFile(
        id=2,
        source_id=SOURCE_ID,
        file_name="0801.mp4",
        file_path="/seed/videos/living_room/0801.mp4",
        file_path_hash="seed_hash_0801",
        storage_type="local_file",
        file_format="mp4",
        start_time=datetime(2026, 3, 14, 8, 1, 0),
        end_time=ANCHOR_SESSION_END,
        duration_seconds=60,
        parse_status="parsed",
    )
    session.add_all([file_a, file_b])

    video_session = VideoSession(
        id=SESSION_ID,
        source_id=SOURCE_ID,
        session_start_time=ANCHOR_SESSION_START,
        session_end_time=ANCHOR_SESSION_END,
        total_duration_seconds=120,
        merge_rule="gap_1s",
        analysis_status="success",
        summary_text="早间客厅活动",
        last_analyzed_at=datetime(2026, 3, 14, 8, 5, 0),
    )
    session.add(video_session)

    # VideoSessionFileRel 只有裸 FK 列、无 ORM relationship，UoW 无法推断插入顺序；
    # 显式 flush 保证父行先落库（PostgreSQL 会强制 FK，SQLite 内存库不保证暴露该问题）
    session.flush()

    session.add_all(
        [
            VideoSessionFileRel(session_id=SESSION_ID, video_file_id=1, sort_index=0),
            VideoSessionFileRel(session_id=SESSION_ID, video_file_id=2, sort_index=1),
        ]
    )
    session.flush()

    event_a = EventRecord(
        id=1,
        source_id=SOURCE_ID,
        session_id=SESSION_ID,
        event_start_time=ANCHOR_EVENT_MORNING,
        event_end_time=datetime(2026, 3, 14, 8, 0, 50),
        object_type="pet",
        action_type="jump",
        description="猫跳上沙发",
        event_type="pet_activity",
        title="猫跳上沙发",
    )
    event_b = EventRecord(
        id=2,
        source_id=SOURCE_ID,
        session_id=SESSION_ID,
        event_start_time=ANCHOR_EVENT_CROSS_DAY,
        event_end_time=datetime(2026, 3, 14, 23, 56, 0),
        object_type="person",
        action_type="enter",
        description="成员深夜回家",
        event_type="member_inout",
        title="成员回家",
    )
    session.add_all([event_a, event_b])

    task_log = TaskLog(
        id=1,
        task_type="session_analysis",
        task_target_id=SESSION_ID,
        dedupe_key="seed:analysis:1",
        status="success",
        started_at=ANCHOR_TASK_STARTED,
        finished_at=datetime(2026, 3, 14, 8, 5, 0),
        retry_count=0,
    )
    session.add(task_log)

    daily_summary = DailySummary(
        id=1,
        summary_date=ANCHOR_SUMMARY_DATE,
        summary_title="2026-03-14 家庭日报",
        overall_summary="当日共 2 起事件。",
        event_count=2,
        generated_at=ANCHOR_SUMMARY_GENERATED_AT,
    )
    session.add(daily_summary)

    session.flush()
    return dict(EXPECTED_ROW_COUNTS)


def verify_pre_remediation(session: Session) -> dict[str, int]:
    """校验快照行数；不匹配时抛出 AssertionError。返回实际行数。"""

    tables: dict[str, Any] = {
        "video_source": VideoSource,
        "video_file": VideoFile,
        "video_session": VideoSession,
        "video_session_file_rel": VideoSessionFileRel,
        "event_record": EventRecord,
        "task_log": TaskLog,
        "daily_summary": DailySummary,
    }
    actual = {
        name: int(session.execute(select(func.count()).select_from(model)).scalar_one())
        for name, model in tables.items()
    }
    if actual != EXPECTED_ROW_COUNTS:
        raise AssertionError(
            f"pre-remediation 快照行数校验失败（seed version={SEED_VERSION}）："
            f"expected={EXPECTED_ROW_COUNTS}, actual={actual}"
        )
    return actual
