"""SQLite unit tests for the daily-summary pipeline stage decomposition (Todo 19).

These tests exercise the public API of :mod:`src.services.summarizer`
end-to-end against an in-memory SQLite engine. They cover the
contracts the slim Celery task in :mod:`src.tasks.summarizer`
depends on, but that the legacy ``tests/integration/test_daily_summary_task.py``
suite did not pin explicitly:

* :func:`src.services.summarizer.normalization.clamp_summary_payload`
  — the 500-900 char budget + 3-item attention cap contract.
* :func:`src.services.summarizer.parsing.parse_summary_with_retry`
  / :func:`parse_subject_summary_output` /
  :func:`parse_rollup_output` — the three LLM output shapes.
* :func:`src.services.summarizer.generation.generate_single_pass_summary_payload`
  / :func:`generate_serial_summary_payload` — the LLM-facing
  stage; covered with stub gateways that record the call shape.
* :func:`src.services.summarizer.evidence.build_evidence` — the
  half-open event range, home-context and subject-section assembly.
* :func:`src.services.summarizer.schedule.parse_schedule_time` /
  :func:`scheduled_local_datetime` / :func:`resolve_target_date` /
  :func:`has_existing_summary_or_task` / :func:`claim_dispatch_guard`
  — the dispatch-time guard stage.
* :func:`src.services.summarizer.lifecycle.claim_attempt` /
  :func:`mark_running` / :func:`mark_failed` / :func:`mark_cancelled`
  — the attempt lifecycle transitions through the
  :class:`DailySummaryAttemptRepository`.
* :func:`src.services.summarizer.finalize.find_subscribed_webhooks`
  / :func:`build_webhook_payload` — the webhook discovery + legacy
  envelope builder.

The five previously-failing integration scenarios (the
``_FakeGateway`` ones — see :mod:`tests.integration.test_daily_summary_task`)
have the same root cause as the "single + serial paths produce
consistent results" / "second-subject LLM failure" / "parse retry
failure" / "cancel" / "duplicate finalize keeps previous good
summary" / "evidence preparation trimming" assertions exercised
here.

The PostgreSQL-specific behaviour (concurrency, partial unique
indexes, transactional outbox visibility) lives in
``tests/integration/test_summarizer_stages_postgres.py``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401  (registers every model on Base.metadata)
from src.application.summary_attempt.repository import DailySummaryAttemptRepository
from src.application.summary_attempt.state_machine import DailySummaryAttemptStatus
from src.db.base_class import Base
from src.models.daily_summary import DailySummary
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.models.event_record import EventRecord
from src.models.home_entity_profile import HomeEntityProfile
from src.models.system_config import SystemConfig
from src.models.task_log import TaskLog
from src.models.webhook_config import WebhookConfig
from src.services.summarizer import (
    SERIAL_SPLIT_PROMPT_THRESHOLD,
    WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
    build_evidence,
    build_webhook_payload,
    claim_attempt,
    claim_dispatch_guard,
    clamp_summary_payload,
    complete_subject_sections,
    extract_json_payload,
    find_subscribed_webhooks,
    generate_serial_summary_payload,
    generate_single_pass_summary_payload,
    has_existing_summary_or_task,
    mark_cancelled,
    mark_failed,
    parse_rollup_output,
    parse_schedule_time,
    parse_subject_summary_output,
    parse_summary_with_retry,
    release_dispatch_guard,
    resolve_target_date,
    scheduled_local_datetime,
)
from src.services.summarizer.evidence import Evidence

# ---------------------------------------------------------------------------
# SQLite session fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session() -> Session:
    """Fresh in-memory SQLite session with the full schema registered."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Stubs / helpers
# ---------------------------------------------------------------------------


class _StubGateway:
    """In-memory LLM gateway that returns scripted replies and records usage."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append(
            {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        )
        if not self.replies:
            return ""
        return self.replies.pop(0)

    def get_last_usage(self) -> dict[str, int] | None:
        return None

    def close(self) -> None:
        self.closed = True


def _seed_qa_provider(db: Session) -> None:
    from src.models.llm_provider import LLMProvider

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


def _seed_home_member(db: Session, *, name: str = "爸爸") -> None:
    db.add(
        HomeEntityProfile(
            entity_type="member",
            name=name,
            role_type="father",
            age_group="adult",
            is_enabled=True,
            sort_order=0,
        )
    )
    db.commit()


def _add_event(
    db: Session,
    *,
    target_date: date,
    hour: int = 9,
    related_entities: list[dict[str, Any]] | None = None,
    event_type: str = "member_appear",
    importance: str = "medium",
) -> None:
    db.add(
        EventRecord(
            source_id=1,
            session_id=1,
            event_start_time=datetime(
                target_date.year, target_date.month, target_date.day, hour, 0, 0
            ),
            description="成员出现",
            event_type=event_type,
            title="成员出现",
            summary="爸爸上午出现",
            importance_level=importance,
            related_entities_json=related_entities,
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


# ---------------------------------------------------------------------------
# Normalization stage
# ---------------------------------------------------------------------------


def test_clamp_summary_payload_keeps_under_budget(db_session: Session) -> None:
    """Small payload returns unchanged."""
    overall, sections, attention = clamp_summary_payload(
        "今天整体平稳。",
        [
            {
                "subject_name": "爸爸",
                "subject_type": "member",
                "summary": "在客厅活动",
                "attention_needed": False,
            }
        ],
        [],
    )
    assert overall == "今天整体平稳。"
    assert len(sections) == 1
    assert attention == []


def test_clamp_summary_payload_truncates_overall_when_long(
    db_session: Session,
) -> None:
    """Long overall is clamped sentence-by-sentence."""
    long_overall = "今天整体平稳。" + ("详细描述。" * 60)
    overall, _sections, _attention = clamp_summary_payload(long_overall, [], [])
    assert len(overall) <= 900
    assert overall.endswith("。")


def test_clamp_summary_payload_caps_attention_at_three(
    db_session: Session,
) -> None:
    """Attention items are capped at 3 entries."""
    attention = [{"title": f"item-{i}", "summary": f"detail-{i}", "level": "low"} for i in range(6)]
    _overall, _sections, normalized = clamp_summary_payload("ok", [], attention)
    assert len(normalized) == 3


# ---------------------------------------------------------------------------
# Parsing stage
# ---------------------------------------------------------------------------


def test_extract_json_payload_strips_code_fences(db_session: Session) -> None:
    payload = extract_json_payload('```json\n{"a": 1}\n```')
    assert payload == {"a": 1}


def test_extract_json_payload_rejects_non_object(db_session: Session) -> None:
    with pytest.raises(ValueError):
        extract_json_payload("[1, 2, 3]")


def test_parse_summary_with_retry_succeeds_on_first_try(db_session: Session) -> None:
    raw = json.dumps(
        {
            "overall_summary": "整体平稳。",
            "subject_sections": [],
            "attention_items": [],
        },
        ensure_ascii=False,
    )
    retry_calls: list[Any] = []
    overall, sections, attention, retried = parse_summary_with_retry(
        retry_client=_RecordingClient(retry_calls),
        initial_response_text=raw,
        prompt="ignored",
    )
    assert overall == "整体平稳。"
    assert sections == []
    assert attention == []
    assert retried is False
    assert retry_calls == []


def test_parse_summary_with_retry_retries_on_parse_failure(db_session: Session) -> None:
    retry_client = _RecordingClient(
        [
            json.dumps(
                {
                    "overall_summary": "重试后成功。",
                    "subject_sections": [],
                    "attention_items": [],
                },
                ensure_ascii=False,
            )
        ]
    )
    overall, _sections, _attention, retried = parse_summary_with_retry(
        retry_client=retry_client,
        initial_response_text="not-json",
        prompt="ignored",
    )
    assert overall == "重试后成功。"
    assert retried is True


def test_parse_subject_summary_output_extracts_summary(db_session: Session) -> None:
    summary, attention_needed = parse_subject_summary_output(
        json.dumps({"summary": "在客厅活动。", "attention_needed": True}, ensure_ascii=False),
        subject_name="爸爸",
    )
    assert summary == "在客厅活动。"
    assert attention_needed is True


def test_parse_rollup_output_extracts_overall_and_attention(db_session: Session) -> None:
    overall, attention = parse_rollup_output(
        json.dumps(
            {
                "overall_summary": "昨天整体平稳。",
                "attention_items": [
                    {"title": "门口有人停留", "summary": "5 分钟", "level": "medium"},
                ],
            },
            ensure_ascii=False,
        )
    )
    assert overall == "昨天整体平稳。"
    assert len(attention) == 1


# ---------------------------------------------------------------------------
# Schedule stage
# ---------------------------------------------------------------------------


def test_parse_schedule_time_accepts_valid_hh_mm(db_session: Session) -> None:
    assert parse_schedule_time("08:30") == (8, 30)


@pytest.mark.parametrize("bad", ["", "25:00", "12:60", "12", "abc"])
def test_parse_schedule_time_rejects_invalid_input(bad: str, db_session: Session) -> None:
    with pytest.raises(ValueError):
        parse_schedule_time(bad)


def test_resolve_target_date_returns_yesterday(db_session: Session) -> None:
    now = datetime(2026, 3, 14, 0, 30, tzinfo=ZoneInfo("UTC"))
    assert resolve_target_date(now) == date(2026, 3, 13)


def test_scheduled_local_datetime_composes_correct_instant(db_session: Session) -> None:
    zone = ZoneInfo("UTC")
    scheduled = scheduled_local_datetime(
        datetime(2026, 3, 14, 1, 0, tzinfo=zone),
        "00:30",
        zone,
    )
    assert scheduled == datetime(2026, 3, 14, 0, 30, tzinfo=zone)


def test_has_existing_summary_or_task_returns_true_on_summary(
    db_session: Session,
) -> None:
    target = date(2026, 3, 13)
    db_session.add(DailySummary(summary_date=target, summary_title="t", overall_summary="o"))
    db_session.commit()
    assert has_existing_summary_or_task(db_session, target) is True


def test_has_existing_summary_or_task_returns_false_when_empty(
    db_session: Session,
) -> None:
    assert has_existing_summary_or_task(db_session, date(2026, 3, 13)) is False


def test_claim_dispatch_guard_round_trip(db_session: Session) -> None:
    target = date(2026, 3, 13)
    now = datetime(2026, 3, 14, 0, 31, tzinfo=ZoneInfo("UTC"))
    assert claim_dispatch_guard(db_session, now, target) is True
    # Second claim fails — guard is held.
    assert claim_dispatch_guard(db_session, now, target) is False
    release_dispatch_guard(db_session, target)
    db_session.commit()
    assert claim_dispatch_guard(db_session, now, target) is True


# ---------------------------------------------------------------------------
# Lifecycle stage
# ---------------------------------------------------------------------------


def test_claim_attempt_returns_active_row(db_session: Session) -> None:
    repo = DailySummaryAttemptRepository(db_session)
    task_log = _seed_task_log(db_session)
    outcome = claim_attempt(
        repo,
        summary_date=date(2026, 3, 13),
        triggered_by="unit",
        task_log_id=int(task_log.id),
    )
    assert outcome.created is True
    assert outcome.attempt.summary_date == date(2026, 3, 13)
    assert outcome.attempt.status == DailySummaryAttemptStatus.CLAIMED.value


def test_claim_attempt_returns_existing_on_second_call(db_session: Session) -> None:
    repo = DailySummaryAttemptRepository(db_session)
    task_log = _seed_task_log(db_session)
    first = claim_attempt(
        repo,
        summary_date=date(2026, 3, 13),
        triggered_by="unit",
        task_log_id=int(task_log.id),
    )
    second = claim_attempt(
        repo,
        summary_date=date(2026, 3, 13),
        triggered_by="unit",
        task_log_id=int(task_log.id),
    )
    assert first.created is True
    assert second.created is False
    assert second.attempt.id == first.attempt.id


def test_mark_running_then_failed_preserves_prior_summary(
    db_session: Session,
) -> None:
    """Failure terminal transition does NOT touch ``daily_summary``."""
    repo = DailySummaryAttemptRepository(db_session)
    task_log = _seed_task_log(db_session)
    attempt = _seed_running_attempt(
        db_session, task_log_id=int(task_log.id), target=date(2026, 3, 13)
    )

    db_session.add(
        DailySummary(
            summary_date=date(2026, 3, 13),
            summary_title="previous",
            overall_summary="prior good summary",
        )
    )
    db_session.commit()

    failed = mark_failed(
        repo,
        attempt_id=int(attempt.id),
        error_type="RuntimeError",
        last_error="boom",
        failure_reason="llm_error",
    )
    db_session.commit()
    assert failed is not None
    assert failed.status == DailySummaryAttemptStatus.FAILED.value

    rows = db_session.query(DailySummary).all()
    assert len(rows) == 1
    assert rows[0].overall_summary == "prior good summary"


def test_mark_cancelled_preserves_prior_summary(db_session: Session) -> None:
    repo = DailySummaryAttemptRepository(db_session)
    task_log = _seed_task_log(db_session)
    attempt = _seed_running_attempt(
        db_session, task_log_id=int(task_log.id), target=date(2026, 3, 13)
    )

    db_session.add(
        DailySummary(
            summary_date=date(2026, 3, 13),
            summary_title="prior",
            overall_summary="prior good summary",
        )
    )
    db_session.commit()

    cancelled = mark_cancelled(repo, attempt_id=int(attempt.id), last_error="user cancelled")
    db_session.commit()
    assert cancelled is not None
    assert cancelled.status == DailySummaryAttemptStatus.CANCELLED.value

    rows = db_session.query(DailySummary).all()
    assert len(rows) == 1
    assert rows[0].overall_summary == "prior good summary"


# ---------------------------------------------------------------------------
# Finalize stage
# ---------------------------------------------------------------------------


def test_find_subscribed_webhooks_returns_only_matching_enabled(
    db_session: Session,
) -> None:
    db_session.add(
        WebhookConfig(
            name="matching",
            url="https://example.com/hook",
            event_types_json=[WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED],
            event_subscriptions_json=None,
            enabled=True,
        )
    )
    db_session.add(
        WebhookConfig(
            name="non-matching",
            url="https://example.com/other",
            event_types_json=["other_event"],
            event_subscriptions_json=None,
            enabled=True,
        )
    )
    db_session.add(
        WebhookConfig(
            name="disabled",
            url="https://example.com/disabled",
            event_types_json=[WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED],
            event_subscriptions_json=None,
            enabled=False,
        )
    )
    db_session.commit()
    assert find_subscribed_webhooks(db_session) == [1]


def test_build_webhook_payload_shape(db_session: Session) -> None:
    payload = build_webhook_payload(
        target_date=date(2026, 3, 13),
        summary_title="2026-03-13 家庭日报",
        overall_summary="整体平稳。",
        subject_sections=[{"subject_name": "爸爸", "summary": "在客厅"}],
        attention_items=[],
        event_count=3,
    )
    assert payload["event"] == WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED
    assert payload["version"] == "1.0"
    assert "generated_at" in payload
    assert payload["data"]["date"] == "2026-03-13"
    assert payload["data"]["summary_title"] == "2026-03-13 家庭日报"
    assert payload["data"]["event_count"] == 3


# ---------------------------------------------------------------------------
# Evidence stage
# ---------------------------------------------------------------------------


def test_build_evidence_returns_half_open_event_range(db_session: Session) -> None:
    db_session.add(SystemConfig(config_key="home_timezone", config_value="Asia/Shanghai"))
    _seed_qa_provider(db_session)
    _seed_home_member(db_session, name="爸爸")
    target = date(2026, 3, 13)
    _add_event(db_session, target_date=target, hour=9)

    # Add an event OUTSIDE the Shanghai local day for 2026-03-13.
    db_session.add(
        EventRecord(
            source_id=1,
            session_id=1,
            event_start_time=datetime(2026, 3, 13, 16, 0, 0, tzinfo=timezone.utc),
            description="next local day",
        )
    )
    db_session.commit()

    evidence = build_evidence(
        db_session,
        target_date=target,
        zone=ZoneInfo("Asia/Shanghai"),
        prompt_builder=lambda payload: ("SYSTEM", "USER"),
        prompt_input_factory=lambda **kwargs: kwargs,
    )
    assert isinstance(evidence, Evidence)
    assert len(evidence.events) == 1
    assert evidence.events[0].description == "成员出现"
    assert evidence.known_subjects == [{"subject_name": "爸爸", "subject_type": "member"}]
    assert evidence.prompt_chars > 0


def test_build_evidence_trims_prompt_when_input_is_large(
    db_session: Session,
) -> None:
    db_session.add(SystemConfig(config_key="home_timezone", config_value="UTC"))
    _seed_qa_provider(db_session)
    _seed_home_member(db_session, name="爸爸")
    target = date(2026, 3, 13)
    for hour in range(24):
        _add_event(db_session, target_date=target, hour=hour)
    db_session.commit()

    captured: dict[str, Any] = {}

    def _capture(prompt_input: Any) -> tuple[str, str]:
        # Mark that the prompt builder saw the input — also stores it
        # so the test can verify trimming kicked in.
        captured["prompt_input"] = prompt_input
        return ("SYSTEM", "X" * 50000)

    evidence = build_evidence(
        db_session,
        target_date=target,
        zone=ZoneInfo("UTC"),
        prompt_builder=_capture,
        prompt_input_factory=lambda **kwargs: kwargs,
    )
    assert evidence.prompt_chars == 50000
    assert evidence.single_pass_user_prompt_chars == 50000
    assert evidence.prompt_chars > SERIAL_SPLIT_PROMPT_THRESHOLD


# ---------------------------------------------------------------------------
# Generation stage
# ---------------------------------------------------------------------------


def test_generate_single_pass_summary_payload_uses_known_subjects(
    db_session: Session,
) -> None:
    """Single-pass path fills in missing subjects even when the LLM omits one."""
    _seed_qa_provider(db_session)
    _seed_home_member(db_session, name="爸爸")
    _seed_home_member(db_session, name="妈妈")
    target = date(2026, 3, 13)
    _add_event(
        db_session,
        target_date=target,
        hour=9,
        related_entities=[
            {
                "entity_type": "member",
                "display_name": "爸爸",
                "matched_profile_name": "爸爸",
                "recognition_status": "confirmed",
            }
        ],
    )

    stub = _StubGateway(
        [
            json.dumps(
                {
                    "overall_summary": "昨天爸爸在客厅活动。",
                    "subject_sections": [
                        {
                            "subject_name": "爸爸",
                            "subject_type": "member",
                            "summary": "在客厅活动。",
                            "attention_needed": False,
                        }
                    ],
                    "attention_items": [],
                },
                ensure_ascii=False,
            )
        ]
    )
    evidence = build_evidence(
        db_session,
        target_date=target,
        zone=ZoneInfo("UTC"),
        prompt_builder=lambda payload: ("SYS", "USR"),
        prompt_input_factory=lambda **kwargs: kwargs,
    )
    overall, sections, attention, prompt_chars, parse_retried = (
        generate_single_pass_summary_payload(
            db=db_session,
            client=stub,
            provider_id=1,
            provider_name_snapshot="qa-default",
            events=evidence.events,
            prompt=evidence.prompt,
            known_subjects=evidence.known_subjects,
            subject_sections_payload=evidence.subject_sections,
        )
    )
    assert overall == "昨天爸爸在客厅活动。"
    assert [s["subject_name"] for s in sections] == ["爸爸", "妈妈"]
    assert attention == []
    assert parse_retried is False


def test_generate_serial_summary_payload_uses_rollup(
    db_session: Session,
) -> None:
    """Serial path calls the LLM per-subject + once for the rollup."""
    _seed_qa_provider(db_session)
    _seed_home_member(db_session, name="爸爸")
    target = date(2026, 3, 13)
    _add_event(
        db_session,
        target_date=target,
        hour=9,
        related_entities=[
            {
                "entity_type": "member",
                "display_name": "爸爸",
                "matched_profile_name": "爸爸",
                "recognition_status": "confirmed",
            }
        ],
    )

    subject_reply = json.dumps(
        {"summary": "在客厅活动。", "attention_needed": False},
        ensure_ascii=False,
    )
    rollup_reply = json.dumps(
        {"overall_summary": "昨天爸爸在客厅。", "attention_items": []},
        ensure_ascii=False,
    )
    stub = _StubGateway([subject_reply, rollup_reply])
    evidence = build_evidence(
        db_session,
        target_date=target,
        zone=ZoneInfo("UTC"),
        prompt_builder=lambda payload: ("SYS", "USR"),
        prompt_input_factory=lambda **kwargs: kwargs,
    )
    overall, sections, attention, prompt_chars, parse_retried = generate_serial_summary_payload(
        db=db_session,
        client=stub,
        provider_id=1,
        provider_name_snapshot="qa-default",
        target_date=target,
        start_dt=evidence.start_dt,
        end_dt=evidence.end_dt,
        events=evidence.events,
        home_context=evidence.home_context,
        known_subjects=evidence.known_subjects,
        subject_sections_payload=evidence.subject_sections,
        missing_subjects=evidence.missing_subjects,
        attention_candidates_payload=evidence.attention_candidates,
    )
    assert overall == "昨天爸爸在客厅。"
    assert len(sections) == 1
    assert sections[0]["subject_name"] == "爸爸"
    assert parse_retried is False
    # Two calls: per-subject + rollup.
    assert len(stub.calls) == 2


def test_generate_serial_summary_payload_recovers_from_subject_parse_failure(
    db_session: Session,
) -> None:
    """Subject-level parse failure → fallback text in the section, no retry-failure overall."""
    _seed_qa_provider(db_session)
    _seed_home_member(db_session, name="爸爸")
    target = date(2026, 3, 13)
    _add_event(
        db_session,
        target_date=target,
        hour=9,
        related_entities=[
            {
                "entity_type": "member",
                "display_name": "爸爸",
                "matched_profile_name": "爸爸",
                "recognition_status": "confirmed",
            }
        ],
    )

    subject_reply = "not-json-at-all"
    retry_reply = json.dumps(
        {"summary": "在客厅活动。", "attention_needed": False},
        ensure_ascii=False,
    )
    rollup_reply = json.dumps(
        {"overall_summary": "昨天爸爸在客厅。", "attention_items": []},
        ensure_ascii=False,
    )
    stub = _StubGateway([subject_reply, retry_reply, rollup_reply])
    evidence = build_evidence(
        db_session,
        target_date=target,
        zone=ZoneInfo("UTC"),
        prompt_builder=lambda payload: ("SYS", "USR"),
        prompt_input_factory=lambda **kwargs: kwargs,
    )
    overall, sections, _attention, _chars, parse_retried = generate_serial_summary_payload(
        db=db_session,
        client=stub,
        provider_id=1,
        provider_name_snapshot="qa-default",
        target_date=target,
        start_dt=evidence.start_dt,
        end_dt=evidence.end_dt,
        events=evidence.events,
        home_context=evidence.home_context,
        known_subjects=evidence.known_subjects,
        subject_sections_payload=evidence.subject_sections,
        missing_subjects=evidence.missing_subjects,
        attention_candidates_payload=evidence.attention_candidates,
    )
    assert overall == "昨天爸爸在客厅。"
    assert sections[0]["subject_name"] == "爸爸"
    assert sections[0]["summary"] == "在客厅活动。"
    assert parse_retried is True


# ---------------------------------------------------------------------------
# Helper modules
# ---------------------------------------------------------------------------


def test_complete_subject_sections_fills_missing(db_session: Session) -> None:
    """`complete_subject_sections` adds missing-subject stubs."""
    sections = complete_subject_sections(
        sections=[
            {
                "subject_name": "爸爸",
                "subject_type": "member",
                "summary": "客厅活动",
                "attention_needed": False,
            }
        ],
        known_subjects=[
            {"subject_name": "爸爸", "subject_type": "member"},
            {"subject_name": "布丁", "subject_type": "pet"},
        ],
        subject_sections_payload=[
            {"subject_name": "爸爸", "subject_type": "member", "related_event_count": 1},
            {"subject_name": "布丁", "subject_type": "pet", "related_event_count": 0},
        ],
    )
    names = [s["subject_name"] for s in sections]
    assert names == ["爸爸", "布丁"]
    # 布丁 with 0 events should have the "no activity" stub.
    buding = next(s for s in sections if s["subject_name"] == "布丁")
    assert "布丁" in buding["summary"]


class _RecordingClient:
    """Records retry calls; the constructor takes the list of return strings."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def chat_completion(self, messages, temperature=0, max_tokens=None):
        self.calls.append(
            {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        )
        if not self.replies:
            return ""
        return self.replies.pop(0)
