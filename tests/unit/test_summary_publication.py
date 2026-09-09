"""PostgreSQL unit tests for the daily-summary publication use case (Todo 16).

These tests use the ``pg_db`` fixture from ``tests/conftest.py`` — the
schema is built once per run by ``alembic upgrade head`` and every test
rolls back through the outer-transaction mechanism, so each test stands
on a clean baseline without per-test ``create_all`` cost.

They exercise :func:`publish_daily_summary` against the full model
surface (``daily_summary``, ``daily_summary_generation_attempt``,
``task_log``, ``outbox_event``) and verify the atomicity contract from
ADR ``docs/adr/0011-transactional-outbox-and-task-lifecycle.md`` §1 —
the three writes (upsert summary + finalise attempt + enroll outbox)
share one transaction, so a ``rollback()`` leaves zero rows behind.

PostgreSQL-specific concurrency / deadlock / JSONB semantics live in
``tests/integration/test_summary_publication_postgres.py``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxStatus
from src.application.summary_attempt.repository import DailySummaryAttemptRepository
from src.application.summary_attempt.state_machine import DailySummaryAttemptStatus
from src.application.summary_publication import (
    EMPTY_DAY_FALLBACK_TEXT,
    AttemptNotInValidStateError,
    PublishDailySummaryCommand,
    publish_daily_summary,
)
from src.models.daily_summary import DailySummary
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    """Clear the in-memory emitted-event-id cache between tests."""
    outbox_contracts._reset_emitted_event_ids_for_testing()


def _new_task_log(db: Session, *, task_type: str = "daily_summary_generation") -> TaskLog:
    """Insert a minimal ``TaskLog`` and commit so it has a server-assigned id."""
    task_log = TaskLog(task_type=task_type, status="running")
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _new_running_attempt(
    db: Session,
    *,
    task_log_id: int,
    target: date,
    attempt_no: int = 1,
) -> DailySummaryGenerationAttempt:
    """Insert a ``daily_summary_generation_attempt`` in ``running`` state."""
    now = datetime.now(tz=timezone.utc)
    attempt = DailySummaryGenerationAttempt(
        summary_date=target,
        attempt_no=attempt_no,
        status=DailySummaryAttemptStatus.RUNNING.value,
        task_log_id=task_log_id,
        triggered_by="unit",
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
    db: Session,
    *,
    events_count: int = 3,
    overall: str = "今日整体平稳。",
    title: str = "家庭日报 2026-09-02",
) -> dict[str, Any]:
    """Build a sample ``summary_content_json`` payload.

    Creates (and commits) a real ``LLMProvider`` row so the FK on
    ``daily_summary.provider_id`` resolves on PostgreSQL.
    """
    from src.models.llm_provider import LLMProvider

    provider = LLMProvider(
        provider_name="openai-compatible-test",
        provider_type="qa_provider",
        api_base_url="https://example.com/v1",
        api_key="dummy",
        model_name="gpt-4o-mini",
        enabled=True,
        supports_qa=True,
        is_default_qa=True,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)

    return {
        "summary_title": title,
        "overall_summary": overall,
        "subject_sections": [
            {"subject_key": "cat", "items": [{"title": "跳上桌子"}]},
        ],
        "attention_items": [
            {"title": "门口有人停留 5 分钟"},
        ],
        "events_count": events_count,
        "provider_id": int(provider.id),
        "provider_name_snapshot": "openai-compatible-test",
    }


def _cmd(
    db_session: Session,
    attempt: DailySummaryGenerationAttempt,
    task_log_id: int,
    *,
    subscribers: list[int] | None = None,
    content: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> PublishDailySummaryCommand:
    """Build a ``PublishDailySummaryCommand`` for ``attempt``."""
    return PublishDailySummaryCommand(
        summary_date=attempt.summary_date,
        attempt_id=attempt.id,
        task_log_id=task_log_id,
        summary_content_json=content if content is not None else _sample_content(db_session),
        webhook_subscribers=list(subscribers or []),
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_publish_succeeds_atomically_with_no_subscribers(pg_db: Session) -> None:
    """The atomic write produces one summary + one attempt + zero outbox rows."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))

    outcome = publish_daily_summary(pg_db, _cmd(pg_db, attempt, task_log.id))
    pg_db.commit()

    assert outcome.summary_id > 0
    assert outcome.attempt_status == DailySummaryAttemptStatus.SUCCEEDED
    assert outcome.webhook_event_ids == []

    summary = pg_db.query(DailySummary).filter(DailySummary.summary_date == date(2026, 9, 2)).one()
    assert summary.id == outcome.summary_id
    assert summary.overall_summary == "今日整体平稳。"
    assert summary.event_count == 3

    finalised_attempt = (
        pg_db.query(DailySummaryGenerationAttempt)
        .filter(DailySummaryGenerationAttempt.id == attempt.id)
        .one()
    )
    assert finalised_attempt.status == DailySummaryAttemptStatus.SUCCEEDED.value
    assert finalised_attempt.finished_at is not None
    assert finalised_attempt.failure_reason is None

    assert pg_db.query(OutboxEventRow).count() == 0
    assert pg_db.query(TaskLog).count() == 1  # the attempt's bound log only


def test_publish_upserts_existing_summary_for_same_date(pg_db: Session) -> None:
    """A second publish for the same date updates the row in place."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))

    first_outcome = publish_daily_summary(pg_db, _cmd(pg_db, attempt, task_log.id))
    pg_db.commit()
    first_id = first_outcome.summary_id
    first_generated_at = (
        pg_db.query(DailySummary)
        .filter(DailySummary.summary_date == date(2026, 9, 2))
        .one()
        .generated_at
    )

    # A fresh attempt for the same date — simulates the operator
    # clicking "re-generate" after the first one succeeded.
    second_attempt = _new_running_attempt(
        pg_db,
        task_log_id=task_log.id,
        target=date(2026, 9, 2),
        attempt_no=2,
    )
    new_content = _sample_content(pg_db, events_count=5, overall="已更新。", title="家庭日报 v2")
    second_outcome = publish_daily_summary(
        pg_db,
        _cmd(
            pg_db,
            second_attempt,
            task_log.id,
            content=new_content,
        ),
    )
    pg_db.commit()

    # Same row id — the upsert is keyed on ``summary_date``.
    assert second_outcome.summary_id == first_id

    rows = pg_db.query(DailySummary).filter(DailySummary.summary_date == date(2026, 9, 2)).all()
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].overall_summary == "已更新。"
    assert rows[0].event_count == 5
    # generated_at is refreshed on every publish.
    assert rows[0].generated_at >= first_generated_at


def test_publish_enrolls_webhook_outbox_for_each_subscriber(pg_db: Session) -> None:
    """Three subscribers produce three ``OutboxEvent`` + three ``TaskLog`` rows."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))

    outcome = publish_daily_summary(
        pg_db,
        _cmd(pg_db, attempt, task_log.id, subscribers=[101, 102, 103]),
    )
    pg_db.commit()

    assert len(outcome.webhook_event_ids) == 3

    # 1 attempt-bound log + 3 webhook logs = 4 in total.
    task_logs = pg_db.query(TaskLog).order_by(TaskLog.id).all()
    assert len(task_logs) == 4

    outbox_rows = pg_db.query(OutboxEventRow).all()
    assert len(outbox_rows) == 3
    for row in outbox_rows:
        assert row.status == OutboxStatus.PENDING.value
        assert row.task_name == "src.tasks.webhook.send_webhook_task"
        assert row.queue == "celery"
        # The webhook task payload is JSON-safe.
        assert isinstance(row.args_json, list)
        assert isinstance(row.kwargs_json, dict)
        assert row.kwargs_json["event_type"] == "daily_summary_generated"
        assert "payload" in row.kwargs_json
        assert "webhook_id" in row.kwargs_json["payload"]["data"]

    # Each outbox row maps to a distinct task_log_id (the per-subscriber
    # log), so the partial unique on (task_log_id) WHERE status='pending'
    # cannot block legitimate concurrent webhooks.
    task_log_ids = {row.task_log_id for row in outbox_rows}
    assert len(task_log_ids) == 3


def test_publish_does_not_emit_webhook_when_no_subscribers(pg_db: Session) -> None:
    """Empty subscriber list → no outbox / no extra TaskLog rows."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))

    outcome = publish_daily_summary(
        pg_db,
        _cmd(pg_db, attempt, task_log.id, subscribers=[]),
    )
    pg_db.commit()

    assert outcome.webhook_event_ids == []
    assert pg_db.query(OutboxEventRow).count() == 0
    # The attempt's bound log is still present.
    assert pg_db.query(TaskLog).count() == 1


def test_publish_with_empty_events_count_still_succeeds(pg_db: Session) -> None:
    """Empty-event day still upserts the summary and emits webhooks."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))
    content = _sample_content(pg_db, events_count=0)
    content["overall_summary"] = ""  # trigger the stable fallback path

    outcome = publish_daily_summary(
        pg_db,
        _cmd(
            pg_db,
            attempt,
            task_log.id,
            subscribers=[201, 202],
            content=content,
        ),
    )
    pg_db.commit()

    summary = pg_db.query(DailySummary).filter(DailySummary.summary_date == date(2026, 9, 2)).one()
    assert summary.event_count == 0
    # Stable fallback text fills in when the caller passes empty.
    assert summary.overall_summary == EMPTY_DAY_FALLBACK_TEXT

    # Webhooks still fire so subscribers know "no events today".
    assert len(outcome.webhook_event_ids) == 2
    assert pg_db.query(OutboxEventRow).count() == 2


# ---------------------------------------------------------------------------
# State-machine rejections
# ---------------------------------------------------------------------------


def test_publish_rejects_attempt_in_terminal_failure_state(pg_db: Session) -> None:
    """An already-cancelled attempt raises and leaves no summary."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))
    attempt_repo = DailySummaryAttemptRepository(pg_db)
    assert attempt_repo.mark_cancelled(attempt.id, last_error="unit cancel") is not None
    pg_db.commit()

    with pytest.raises(AttemptNotInValidStateError) as excinfo:
        publish_daily_summary(pg_db, _cmd(pg_db, attempt, task_log.id))
    assert "cancelled" in str(excinfo.value)
    assert excinfo.value.payload["current_status"] == "cancelled"

    # No summary was written and the attempt is still cancelled (we did
    # not commit the failed publish transaction).
    assert pg_db.query(DailySummary).count() == 0
    assert pg_db.query(OutboxEventRow).count() == 0


def test_publish_rejects_attempt_in_superseded_state(pg_db: Session) -> None:
    """An attempt already marked ``superseded`` cannot transition to ``succeeded``."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))
    attempt_repo = DailySummaryAttemptRepository(pg_db)
    assert attempt_repo.mark_superseded(attempt.id) is not None
    pg_db.commit()

    with pytest.raises(AttemptNotInValidStateError) as excinfo:
        publish_daily_summary(pg_db, _cmd(pg_db, attempt, task_log.id))
    assert excinfo.value.payload["current_status"] == "superseded"
    assert pg_db.query(DailySummary).count() == 0


def test_publish_rejects_attempt_already_succeeded(pg_db: Session) -> None:
    """A second publish for an already-``succeeded`` attempt is rejected.

    The state-machine guard ``running → succeeded`` is the only legal
    edge, so re-running the use case after success must raise rather
    than silently overwrite the previous result.
    """
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))

    publish_daily_summary(pg_db, _cmd(pg_db, attempt, task_log.id))
    pg_db.commit()

    # After the first call the attempt is in ``succeeded``; a second
    # call must reject.
    with pytest.raises(AttemptNotInValidStateError) as excinfo:
        publish_daily_summary(pg_db, _cmd(pg_db, attempt, task_log.id))
    assert excinfo.value.payload["current_status"] == "succeeded"


def test_publish_rejects_missing_attempt(pg_db: Session) -> None:
    """A non-existent attempt id raises ``AttemptNotInValidStateError``."""
    with pytest.raises(AttemptNotInValidStateError) as excinfo:
        publish_daily_summary(
            pg_db,
            PublishDailySummaryCommand(
                summary_date=date(2026, 9, 2),
                attempt_id=99999,
                task_log_id=1,
                summary_content_json=_sample_content(pg_db),
                webhook_subscribers=[],
            ),
        )
    assert "does not exist" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Transactional contract
# ---------------------------------------------------------------------------


def test_publish_rollback_leaves_no_summary_no_attempt_no_outbox(
    pg_db: Session,
) -> None:
    """A rollback after publish discards every write.

    The ``pg_db`` fixture wraps every test in an outer BEGIN that the
    fixture rolls back at teardown. The contract under test: the three
    writes (upsert summary + finalise attempt + enroll outbox rows)
    share one transaction, so a single ``rollback()`` discards every
    write — the rows are visible only after an explicit
    ``commit()``. The fixture's outer BEGIN means the setup rows live
    in the same transaction as the publish writes here, so on rollback
    everything disappears together; we therefore verify the publish
    writes vanish (the use case guarantee) rather than the SQLite-era
    "setup stays / publish disappears" split, which was an artefact of
    SQLite's commit being durable at ``session.commit()`` time.
    """
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))

    publish_daily_summary(
        pg_db,
        _cmd(pg_db, attempt, task_log.id, subscribers=[301, 302]),
    )
    assert pg_db.query(DailySummary).count() == 1
    assert pg_db.query(OutboxEventRow).count() == 2

    pg_db.rollback()
    pg_db.expire_all()

    assert pg_db.query(DailySummary).count() == 0
    assert pg_db.query(OutboxEventRow).count() == 0
    assert pg_db.query(DailySummaryGenerationAttempt).count() == 0
    assert pg_db.query(TaskLog).count() == 0


def test_publish_dry_run_enrolls_no_webhooks_but_returns_payload(
    pg_db: Session,
) -> None:
    """``dry_run=True`` returns a payload without enrolling webhook outbox rows."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))

    outcome = publish_daily_summary(
        pg_db,
        _cmd(
            pg_db,
            attempt,
            task_log.id,
            subscribers=[401, 402, 403],
            dry_run=True,
        ),
    )
    pg_db.commit()

    assert outcome.summary_id > 0
    assert outcome.webhook_event_ids == []

    # The summary was upserted and the attempt finalised — the only
    # thing skipped is the webhook outbox enrollment.
    assert pg_db.query(DailySummary).count() == 1
    finalised = (
        pg_db.query(DailySummaryGenerationAttempt)
        .filter(DailySummaryGenerationAttempt.id == attempt.id)
        .one()
    )
    assert finalised.status == DailySummaryAttemptStatus.SUCCEEDED.value
    assert pg_db.query(OutboxEventRow).count() == 0
    # No per-subscriber webhook TaskLog rows either.
    assert pg_db.query(TaskLog).count() == 1


def test_publish_value_error_when_task_log_id_mismatches_attempt(
    pg_db: Session,
) -> None:
    """The use case refuses a command whose ``task_log_id`` does not match."""
    task_log = _new_task_log(pg_db)
    attempt = _new_running_attempt(pg_db, task_log_id=task_log.id, target=date(2026, 9, 2))
    bogus_task_log_id = task_log.id + 999

    with pytest.raises(ValueError, match="task_log_id"):
        publish_daily_summary(
            pg_db,
            _cmd(pg_db, attempt, bogus_task_log_id, content={"provider_id": None}),
        )
    pg_db.rollback()

    assert pg_db.query(DailySummary).count() == 0
    assert pg_db.query(OutboxEventRow).count() == 0
