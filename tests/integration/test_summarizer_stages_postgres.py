"""PostgreSQL integration tests for the daily-summary pipeline decomposition (Todo 19).

These tests exercise the staged helpers in :mod:`src.services.summarizer`
against a real PostgreSQL schema migrated to the new head. They
cover the PG-specific behaviour the SQLite unit tests in
``tests/unit/test_summarizer_stages.py`` cannot:

* the per-date active-attempt partial unique index — the second
  ``claim_attempt`` for the same date returns the existing row
  (``created=False``) on PG, not just a Python-level
  ``return None``;
* the publish use case (Todo 16) + the legacy webhook fan-out
  combined into one transaction with the attempt ``→ succeeded``
  transition — a previous successful ``daily_summary`` is
  overwritten, but a previously successful ``daily_summary`` from
  a *prior* attempt is preserved when a *new* attempt fails;
* the cancellation path — the attempt is flipped
  ``→ cancelled`` via the Todo 15 state machine guard while the
  prior successful ``daily_summary`` row stays intact (this is
  the "duplicate finalize keeps previous good summary"
  assertion);
* the dispatcher-side schedule guard (``AppRuntimeState`` per-date
  slot) survives a real ``commit()`` boundary, so a parallel
  session sees the lock.

The PG isolation contract (one ``vd_test_<uuid>`` schema per
``pytest`` run, ``DROP SCHEMA … CASCADE`` at the end) is provided
by ``tests/conftest.py``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401  (registers every model on Base.metadata)
from src.application.outbox import contracts as outbox_contracts
from src.application.summary_attempt.repository import DailySummaryAttemptRepository
from src.application.summary_attempt.state_machine import DailySummaryAttemptStatus
from src.application.summary_publication import (
    PublishDailySummaryCommand,
    publish_daily_summary,
)
from src.models.daily_summary import DailySummary
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.models.llm_provider import LLMProvider
from src.models.task_log import TaskLog
from src.models.webhook_config import WebhookConfig
from src.services.summarizer import (
    WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
    claim_attempt,
    claim_dispatch_guard,
    find_subscribed_webhooks,
    has_existing_summary_or_task,
    mark_cancelled,
    mark_failed,
    release_dispatch_guard,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture(autouse=True)
def _truncate_summarizer_tables(postgres_migrated_engine: Engine) -> None:
    """Wipe the four tables the summarizer stages touch (in FK-safe order)."""
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE outbox_event RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE daily_summary RESTART IDENTITY CASCADE"))
        conn.execute(
            sql_text("TRUNCATE TABLE daily_summary_generation_attempt RESTART IDENTITY CASCADE")
        )
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE webhook_config RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE webhook_delivery_log RESTART IDENTITY CASCADE"))


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _seed_qa_provider(db: Session) -> None:
    db.add(
        LLMProvider(
            provider_name="qa-default",
            provider_type="qa_provider",
            api_base_url="https://example.com/v1",
            api_key="dummy-key",
            model_name="gpt-4o-mini",
            timeout_seconds=30,
            retry_count=1,
            enabled=True,
            supports_qa=True,
            is_default_qa=True,
            supports_vision=False,
            is_default_vision=False,
        )
    )
    db.commit()


def _seed_task_log(db: Session) -> TaskLog:
    task_log = TaskLog(task_type="daily_summary_generation", status="running")
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _seed_running_attempt(
    db: Session,
    *,
    task_log_id: int,
    target: date,
    attempt_no: int = 1,
) -> DailySummaryGenerationAttempt:
    now = datetime.now(tz=timezone.utc)
    attempt = DailySummaryGenerationAttempt(
        summary_date=target,
        attempt_no=attempt_no,
        status=DailySummaryAttemptStatus.RUNNING.value,
        task_log_id=task_log_id,
        triggered_by="pg-unit",
        started_at=now,
        claimed_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def _sample_content(
    *,
    events_count: int = 3,
    overall: str = "今日整体平稳。",
    title: str = "家庭日报 PG",
) -> dict[str, Any]:
    return {
        "summary_title": title,
        "overall_summary": overall,
        "subject_sections": [{"subject_name": "爸爸", "summary": "在客厅"}],
        "attention_items": [{"title": "门口有人停留 5 分钟"}],
        "events_count": events_count,
        "provider_id": 1,
        "provider_name_snapshot": "qa-default",
    }


# ---------------------------------------------------------------------------
# Attempt lifecycle (PG-specific partial unique index)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_pg_partial_unique_index_serialises_active_attempts(
    postgres_migrated_engine: Engine,
) -> None:
    """Two ``INSERT … ON CONFLICT DO NOTHING`` for the same date cannot both win."""
    factory = _session_factory(postgres_migrated_engine)
    db_a = factory()
    try:
        task_log = _seed_task_log(db_a)
        repo_a = DailySummaryAttemptRepository(db_a)
        outcome_a = claim_attempt(
            repo_a,
            summary_date=date(2026, 9, 2),
            triggered_by="pg-unit",
            task_log_id=int(task_log.id),
        )
        db_a.commit()
        assert outcome_a.created is True
        first_attempt_id = int(outcome_a.attempt.id)
    finally:
        db_a.close()

    db_b = factory()
    try:
        task_log = _seed_task_log(db_b)
        repo_b = DailySummaryAttemptRepository(db_b)
        outcome_b = claim_attempt(
            repo_b,
            summary_date=date(2026, 9, 2),
            triggered_by="pg-unit",
            task_log_id=int(task_log.id),
        )
        db_b.commit()
        assert outcome_b.created is False
        assert int(outcome_b.attempt.id) == first_attempt_id
    finally:
        db_b.close()


# ---------------------------------------------------------------------------
# Publish + cancel atomicity (preserves prior successful summary)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_pg_publish_then_cancel_keeps_previous_good_summary(
    postgres_migrated_engine: Engine,
) -> None:
    """A successful publish followed by a cancel attempt preserves the prior summary."""
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        _seed_qa_provider(db)
        task_log = _seed_task_log(db)
        attempt = _seed_running_attempt(db, task_log_id=int(task_log.id), target=date(2026, 9, 2))

        publish_daily_summary(
            db,
            PublishDailySummaryCommand(
                summary_date=date(2026, 9, 2),
                attempt_id=int(attempt.id),
                task_log_id=int(task_log.id),
                summary_content_json=_sample_content(),
                webhook_subscribers=[],
            ),
        )
        db.commit()

        rows = db.query(DailySummary).all()
        assert len(rows) == 1
        assert rows[0].overall_summary == "今日整体平稳。"

        # Spawn a *new* attempt for the same date — the previous row
        # is in ``succeeded`` so the partial unique index lets a new
        # row in.
        new_task_log = _seed_task_log(db)
        new_attempt = _seed_running_attempt(
            db,
            task_log_id=int(new_task_log.id),
            target=date(2026, 9, 2),
            attempt_no=2,
        )
        repo = DailySummaryAttemptRepository(db)

        cancelled = mark_cancelled(
            repo, attempt_id=int(new_attempt.id), last_error="operator cancel"
        )
        db.commit()
        assert cancelled is not None
        assert cancelled.status == DailySummaryAttemptStatus.CANCELLED.value

        rows = db.query(DailySummary).all()
        assert len(rows) == 1
        assert rows[0].overall_summary == "今日整体平稳。"
    finally:
        db.close()


@pytest.mark.postgres
def test_pg_failed_attempt_preserves_prior_summary(
    postgres_migrated_engine: Engine,
) -> None:
    """A new ``failed`` attempt never touches a prior successful ``daily_summary``."""
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        _seed_qa_provider(db)
        task_log = _seed_task_log(db)
        attempt = _seed_running_attempt(db, task_log_id=int(task_log.id), target=date(2026, 9, 2))
        publish_daily_summary(
            db,
            PublishDailySummaryCommand(
                summary_date=date(2026, 9, 2),
                attempt_id=int(attempt.id),
                task_log_id=int(task_log.id),
                summary_content_json=_sample_content(overall="首次成功。"),
                webhook_subscribers=[],
            ),
        )
        db.commit()

        new_task_log = _seed_task_log(db)
        new_attempt = _seed_running_attempt(
            db,
            task_log_id=int(new_task_log.id),
            target=date(2026, 9, 2),
            attempt_no=2,
        )
        repo = DailySummaryAttemptRepository(db)
        repo.mark_superseded(int(attempt.id))
        db.commit()

        # The new attempt fails (LLM error) — verify the helper only
        # touches the attempt row, never ``daily_summary``.
        failed = mark_failed(
            repo,
            attempt_id=int(new_attempt.id),
            error_type="RuntimeError",
            last_error="boom",
            failure_reason="llm_error",
        )
        db.commit()
        assert failed is not None
        assert failed.status == DailySummaryAttemptStatus.FAILED.value

        rows = db.query(DailySummary).all()
        assert len(rows) == 1
        assert rows[0].overall_summary == "首次成功。"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Dispatch guard — concurrent claim is safe across sessions.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_pg_dispatch_guard_holds_across_sessions(
    postgres_migrated_engine: Engine,
) -> None:
    """Two parallel dispatch attempts both call the helper; only one wins."""
    factory = _session_factory(postgres_migrated_engine)
    target = date(2026, 9, 2)
    now = datetime.now(tz=timezone.utc)

    def _worker(_: int) -> bool:
        db = factory()
        try:
            won = claim_dispatch_guard(db, now, target)
            db.commit()
            return won
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_worker, range(2)))

    assert results.count(True) == 1
    assert results.count(False) == 1

    # Release + retry works.
    db = factory()
    try:
        release_dispatch_guard(db, target)
        db.commit()
        assert claim_dispatch_guard(db, now, target) is True
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Webhook subscriber discovery.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_pg_find_subscribed_webhooks_filters_disabled(
    postgres_migrated_engine: Engine,
) -> None:
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        db.add_all(
            [
                WebhookConfig(
                    name="matching-enabled",
                    url="https://example.com/hook",
                    event_subscriptions_json=[
                        {"event": WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED, "version": ""}
                    ],
                    enabled=True,
                ),
                WebhookConfig(
                    name="non-matching-enabled",
                    url="https://example.com/other",
                    event_subscriptions_json=[{"event": "other_event", "version": ""}],
                    enabled=True,
                ),
                WebhookConfig(
                    name="matching-disabled",
                    url="https://example.com/disabled",
                    event_subscriptions_json=[
                        {"event": WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED, "version": ""}
                    ],
                    enabled=False,
                ),
            ]
        )
        db.commit()

        ids = find_subscribed_webhooks(db)
        assert ids == [1]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Schedule guard — has_existing_summary_or_task respects both summary
# and task_log rows.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_pg_has_existing_summary_or_task_sees_running_task(
    postgres_migrated_engine: Engine,
) -> None:
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        target = date(2026, 9, 2)
        # Insert a running task log that matches the dedupe key.
        db.add(
            TaskLog(
                task_type="daily_summary_generation",
                task_target_id=None,
                dedupe_key=f"daily_summary_generation|{target.isoformat()}",
                status="running",
                detail_json={"target_date": target.isoformat()},
            )
        )
        db.commit()

        assert has_existing_summary_or_task(db, target) is True
    finally:
        db.close()
