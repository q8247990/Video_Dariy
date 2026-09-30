from sqlalchemy.orm import Session

from src.models.outbox import OutboxEvent
from src.models.task_log import TaskLog
from src.models.webhook_config import WebhookConfig
from src.services.analysis.constants import WEBHOOK_EVENT_SESSION_ANALYZED
from src.services.analysis.webhook_events import (
    enroll_session_analyzed_webhooks,
    find_session_analyzed_subscribers,
)
from src.services.pipeline_constants import TaskType


def _hook(db: Session, *, enabled: bool, events: list[str]) -> WebhookConfig:
    hook = WebhookConfig(
        name="hook",
        url="https://example.com/hook",
        enabled=enabled,
        event_subscriptions_json=[{"event": event, "version": "1.0"} for event in events],
    )
    db.add(hook)
    db.flush()
    return hook


def test_find_subscribers_filters_by_event_and_enabled(pg_db: Session) -> None:
    subscribed = _hook(pg_db, enabled=True, events=[WEBHOOK_EVENT_SESSION_ANALYZED])
    _hook(pg_db, enabled=True, events=["daily_summary_generated"])
    _hook(pg_db, enabled=False, events=[WEBHOOK_EVENT_SESSION_ANALYZED])
    pg_db.commit()

    assert find_session_analyzed_subscribers(pg_db) == [subscribed.id]


def test_enroll_session_analyzed_webhooks_creates_tasklog_and_outbox(pg_db: Session) -> None:
    hook = _hook(pg_db, enabled=True, events=[WEBHOOK_EVENT_SESSION_ANALYZED])
    pg_db.commit()

    event_ids = enroll_session_analyzed_webhooks(
        pg_db,
        session_id=7,
        source_id=3,
        events_created=5,
        file_count=2,
        analysis_status="success",
        subscribers=[hook.id],
    )
    pg_db.commit()

    assert len(event_ids) == 1

    task_logs = pg_db.query(TaskLog).filter(TaskLog.task_type == TaskType.WEBHOOK_PUSH).all()
    assert len(task_logs) == 1
    assert task_logs[0].task_target_id == hook.id

    rows = pg_db.query(OutboxEvent).all()
    assert len(rows) == 1
    kwargs = rows[0].kwargs_json
    assert kwargs["event_type"] == WEBHOOK_EVENT_SESSION_ANALYZED
    assert kwargs["webhook_id"] == hook.id
    assert kwargs["payload"]["event"] == WEBHOOK_EVENT_SESSION_ANALYZED
    assert kwargs["payload"]["data"]["session_id"] == 7
    assert kwargs["payload"]["data"]["events_created"] == 5
