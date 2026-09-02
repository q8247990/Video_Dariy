"""PostgreSQL integration tests for the daily-summary publication use case (Todo 16).

These tests exercise :func:`publish_daily_summary` against a real
PostgreSQL schema migrated to the new head. The SQLite unit tests in
``tests/unit/test_summary_publication.py`` cover the algorithmic
contract — these tests pin down the PG-specific behaviour the SQL
dialect owns:

- transactional visibility: uncommitted writes from one session are
  invisible to a second session until the first commits;
- the partial unique index ``(task_log_id) WHERE status='pending'``
  lets legitimate concurrent webhooks share an attempt;
- the ``DailySummary`` ``UNIQUE (summary_date)`` constraint is the
  conflict target of the upsert;
- the ``OutboxEvent`` payload round-trips through ``JSONB`` losslessly;
- the publisher (Todo 13) can claim a webhook outbox row enrolled by
  this use case and mark it ``published`` via the same hot path the
  analyzer / dispatcher rely on.

PG test isolation note
======================

The ``postgres_migrated_engine`` fixture is session-scoped, so all PG
tests in this file share one schema. An autouse fixture
(``_truncate_publication_tables``) wipes ``daily_summary``,
``daily_summary_generation_attempt``, ``outbox_event`` and
``task_log`` between tests so the assertions are deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxStatus
from src.application.outbox.publisher import (
    PUBLISHER_LEASE_SECONDS,
    BrokerPort,
    OutboxPublisher,
    PublisherConfig,
)
from src.application.summary_attempt.repository import DailySummaryAttemptRepository
from src.application.summary_attempt.state_machine import DailySummaryAttemptStatus
from src.application.summary_publication import (
    AttemptNotInValidStateError,
    PublishDailySummaryCommand,
    publish_daily_summary,
)
from src.models.daily_summary import DailySummary
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture(autouse=True)
def _truncate_publication_tables(postgres_migrated_engine: Engine) -> None:
    """Wipe the four tables the publish flow touches.

    Order matters: ``outbox_event`` first (FK to ``task_log`` is
    ``RESTRICT``), then the two summary tables, then ``task_log``
    itself (FK from ``daily_summary_generation_attempt`` is ``SET NULL``
    but we want a clean slate regardless).
    """
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE outbox_event RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE daily_summary RESTART IDENTITY CASCADE"))
        conn.execute(
            sql_text("TRUNCATE TABLE daily_summary_generation_attempt RESTART IDENTITY CASCADE")
        )
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _new_task_log(db: Session) -> TaskLog:
    task_log = TaskLog(task_type="daily_summary_generation", status="running")
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


def _sample_content(*, events_count: int = 3) -> dict[str, Any]:
    return {
        "summary_title": "家庭日报",
        "overall_summary": "今日整体平稳。",
        "subject_sections": [{"subject_key": "cat", "items": [{"title": "跳上桌子"}]}],
        "attention_items": [{"title": "门口有人停留 5 分钟"}],
        "events_count": events_count,
        "provider_id": None,
        "provider_name_snapshot": None,
    }


def _build_cmd(
    *,
    attempt: DailySummaryGenerationAttempt,
    task_log_id: int,
    subscribers: list[int],
    content: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
) -> PublishDailySummaryCommand:
    return PublishDailySummaryCommand(
        summary_date=attempt.summary_date,
        attempt_id=attempt.id,
        task_log_id=task_log_id,
        summary_content_json=content if content is not None else _sample_content(),
        webhook_subscribers=list(subscribers),
        dry_run=dry_run,
    )


@dataclass
class _FakeBroker(BrokerPort):
    """In-memory broker that records every ``send_task`` call."""

    sent_calls: list[dict[str, object]] = field(default_factory=list)

    def send_task(
        self,
        *,
        name: str,
        args: list,
        kwargs: dict,
        queue: str,
        task_id: str,
    ) -> str:
        self.sent_calls.append(
            {
                "name": name,
                "args": list(args),
                "kwargs": dict(kwargs),
                "queue": queue,
                "task_id": task_id,
            }
        )
        return task_id


# ---------------------------------------------------------------------------
# Transactional atomicity
# ---------------------------------------------------------------------------


def test_publish_atomicity_under_pg_transaction(postgres_migrated_engine: Engine) -> None:
    """Uncommitted publish writes are invisible to a second session.

    A concurrent ``SELECT … FROM daily_summary`` from a freshly opened
    session sees zero rows while the publish transaction is still open,
    and exactly one row after the publish transaction commits. The
    same holds for ``outbox_event`` and ``daily_summary_generation_attempt``.
    """
    factory = _session_factory(postgres_migrated_engine)
    publish = factory()
    attempt_id_holder: dict[str, int] = {}
    try:
        task_log = _new_task_log(publish)
        attempt = _new_running_attempt(publish, task_log_id=task_log.id, target=date(2026, 9, 2))
        attempt_id_holder["id"] = attempt.id
        publish_daily_summary(
            publish,
            _build_cmd(attempt=attempt, task_log_id=task_log.id, subscribers=[501, 502]),
        )
        # Do NOT commit yet.

        verify_before = Session(postgres_migrated_engine)
        try:
            assert verify_before.query(DailySummary).count() == 0
            assert verify_before.query(OutboxEventRow).count() == 0
        finally:
            verify_before.close()

        publish.commit()
    finally:
        publish.close()

    verify_after = Session(postgres_migrated_engine)
    try:
        assert verify_after.query(DailySummary).count() == 1
        assert verify_after.query(OutboxEventRow).count() == 2
        finalised = (
            verify_after.query(DailySummaryGenerationAttempt)
            .filter(DailySummaryGenerationAttempt.id == attempt_id_holder["id"])
            .one()
        )
        assert finalised.status == DailySummaryAttemptStatus.SUCCEEDED.value
        assert finalised.finished_at is not None
    finally:
        verify_after.close()


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


def test_publish_does_not_overwrite_previous_success_when_attempt_fails_terminal(
    postgres_migrated_engine: Engine,
) -> None:
    """A terminal-failure on a new attempt preserves the previous summary.

    A previous publish already wrote the daily_summary row for the
    date. A fresh attempt for the same date reaches
    ``running → failed`` (via ``mark_failed``); the publish use case
    is **not** invoked on this path, and the original summary content
    must be byte-for-byte preserved.
    """
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        task_log = _new_task_log(db)
        first_attempt = _new_running_attempt(
            db, task_log_id=task_log.id, target=date(2026, 9, 2), attempt_no=1
        )
        first_content = _sample_content(events_count=4)
        first_outcome = publish_daily_summary(
            db,
            _build_cmd(
                attempt=first_attempt,
                task_log_id=task_log.id,
                subscribers=[],
                content=first_content,
            ),
        )
        db.commit()
        original_summary_id = first_outcome.summary_id
        original_content = (
            db.query(DailySummary).filter(DailySummary.id == original_summary_id).one()
        )
        original_overall = original_content.overall_summary
        original_event_count = original_content.event_count

        # A fresh attempt fails terminal.
        second_attempt = _new_running_attempt(
            db, task_log_id=task_log.id, target=date(2026, 9, 2), attempt_no=2
        )
        attempt_repo = DailySummaryAttemptRepository(db)
        failed = attempt_repo.mark_failed(
            second_attempt.id,
            error_type="ValueError",
            last_error="forced failure",
            failure_reason="llm_error",
        )
        assert failed is not None
        db.commit()

        preserved = (
            db.query(DailySummary).filter(DailySummary.summary_date == date(2026, 9, 2)).one()
        )
        assert preserved.id == original_summary_id
        assert preserved.overall_summary == original_overall
        assert preserved.event_count == original_event_count
        # The second attempt is recorded in the audit history.
        finalised_second = (
            db.query(DailySummaryGenerationAttempt)
            .filter(DailySummaryGenerationAttempt.id == second_attempt.id)
            .one()
        )
        assert finalised_second.status == DailySummaryAttemptStatus.FAILED.value
    finally:
        db.close()


def test_publish_rejects_attempt_in_failed_state(postgres_migrated_engine: Engine) -> None:
    """An attempt in ``failed`` cannot be published.

    The state-machine guard in
    :meth:`DailySummaryAttemptRepository.mark_succeeded` is the
    ultimate enforcer: even if the use case skips its own check, the
    helper returns ``None`` and the use case raises
    :class:`AttemptNotInValidStateError`. The summary is untouched.
    """
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        task_log = _new_task_log(db)
        attempt = _new_running_attempt(db, task_log_id=task_log.id, target=date(2026, 9, 2))
        attempt_repo = DailySummaryAttemptRepository(db)
        assert (
            attempt_repo.mark_failed(
                attempt.id, error_type="X", last_error="x", failure_reason="llm_error"
            )
            is not None
        )
        db.commit()

        with pytest.raises(AttemptNotInValidStateError):
            publish_daily_summary(
                db,
                _build_cmd(attempt=attempt, task_log_id=task_log.id, subscribers=[]),
            )
        db.rollback()
        assert db.query(DailySummary).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Outbox payload invariants
# ---------------------------------------------------------------------------


def test_publish_webhook_outbox_event_ids_unique_per_subscriber(
    postgres_migrated_engine: Engine,
) -> None:
    """Three subscribers produce three distinct ``event_id`` UUIDs."""
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        task_log = _new_task_log(db)
        attempt = _new_running_attempt(db, task_log_id=task_log.id, target=date(2026, 9, 2))
        outcome = publish_daily_summary(
            db,
            _build_cmd(attempt=attempt, task_log_id=task_log.id, subscribers=[11, 22, 33]),
        )
        db.commit()

        assert len(outcome.webhook_event_ids) == 3
        assert len(set(outcome.webhook_event_ids)) == 3

        rows = db.query(OutboxEventRow).all()
        assert len(rows) == 3
        db_event_ids = {row.event_id for row in rows}
        assert len(db_event_ids & set(outcome.webhook_event_ids)) == 3
    finally:
        db.close()


def test_publish_webhook_outbox_payload_is_json_safe(
    postgres_migrated_engine: Engine,
) -> None:
    """The enrolled ``args_json`` / ``kwargs_json`` round-trip cleanly.

    The ``JSONB`` columns store the payload as canonical JSON; a
    round-trip through ``json.dumps`` / ``json.loads`` MUST yield
    equal data (loss equality), and the payload MUST carry the
    target ``webhook_id`` so Todo 19 can teach ``send_webhook_task``
    to filter deliveries.
    """
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        task_log = _new_task_log(db)
        attempt = _new_running_attempt(db, task_log_id=task_log.id, target=date(2026, 9, 2))
        publish_daily_summary(
            db,
            _build_cmd(
                attempt=attempt,
                task_log_id=task_log.id,
                subscribers=[901],
                content=_sample_content(events_count=7),
            ),
        )
        db.commit()

        row = db.query(OutboxEventRow).one()
        # Round-trip via JSON to assert the payload is canonical JSON.
        args_rt = json.loads(json.dumps(row.args_json))
        kwargs_rt = json.loads(json.dumps(row.kwargs_json))
        assert args_rt == []
        assert kwargs_rt["event_type"] == "daily_summary_generated"
        assert kwargs_rt["webhook_id"] == 901
        assert kwargs_rt["payload"]["event"] == "daily_summary_generated"
        assert kwargs_rt["payload"]["version"] == "1.0"
        data = kwargs_rt["payload"]["data"]
        assert data["summary_date"] == "2026-09-02"
        assert data["events_count"] == 7
        assert data["webhook_id"] == 901
        assert "summary_id" in data and isinstance(data["summary_id"], int)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Publisher round-trip
# ---------------------------------------------------------------------------


def test_publish_round_trip_then_outbox_publisher(
    postgres_migrated_engine: Engine,
) -> None:
    """Publish → publisher claims → broker receives → row marked published.

    Asserts the publisher's hot path (Todo 13) consumes the rows the
    publish use case enrolls. The broker receives one ``send_task``
    call per outbox row, with ``task_id=str(event.event_id)``.
    """
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        task_log = _new_task_log(db)
        attempt = _new_running_attempt(db, task_log_id=task_log.id, target=date(2026, 9, 2))
        outcome = publish_daily_summary(
            db,
            _build_cmd(attempt=attempt, task_log_id=task_log.id, subscribers=[701, 702]),
        )
        db.commit()
        expected_event_ids = {str(eid) for eid in outcome.webhook_event_ids}
    finally:
        db.close()

    # Run the publisher twice — ``run_once`` claims one row per call.
    publisher_db = factory()
    broker = _FakeBroker()
    try:
        publisher = OutboxPublisher(
            db_session=publisher_db,
            config=PublisherConfig(
                claimed_by="publisher-publication",
                lease_seconds=PUBLISHER_LEASE_SECONDS,
                max_attempts=3,
            ),
            broker=broker,
        )
        first_delta = publisher.run_once()
        publisher_db.commit()
        second_delta = publisher.run_once()
        publisher_db.commit()
    finally:
        publisher_db.close()

    assert first_delta.published == 1
    assert second_delta.published == 1
    assert {call["task_id"] for call in broker.sent_calls} == expected_event_ids
    for call in broker.sent_calls:
        assert call["name"] == "src.tasks.webhook.send_webhook_task"
        assert call["queue"] == "celery"

    verify = Session(postgres_migrated_engine)
    try:
        rows = (
            verify.query(OutboxEventRow)
            .filter(OutboxEventRow.event_id.in_(list(expected_event_ids)))
            .all()
        )
        assert len(rows) == 2
        for row in rows:
            assert row.status == OutboxStatus.PUBLISHED.value
            assert row.published_at is not None
    finally:
        verify.close()
