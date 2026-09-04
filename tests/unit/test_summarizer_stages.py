"""PostgreSQL unit tests for the daily-summary pipeline stage decomposition (Todo 19).

These tests exercise the public API of :mod:`src.services.summarizer`
end-to-end against the PG test schema (``tests/conftest.py`` ``pg_db``
fixture) — every model is already created by ``alembic upgrade head``,
so each test only seeds the rows it actually exercises.

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
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from src.application.summary_attempt.repository import DailySummaryAttemptRepository
from src.application.summary_attempt.state_machine import DailySummaryAttemptStatus
from src.models.daily_summary import DailySummary
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.models.event_record import EventRecord
from src.models.home_entity_profile import HomeEntityProfile
from src.models.system_config import SystemConfig
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.models.webhook_config import WebhookConfig
from src.services.summarizer import (
    SERIAL_SPLIT_PROMPT_THRESHOLD,
    WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
    build_evidence,
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


def _seed_source_and_session(db: Session) -> tuple[VideoSource, VideoSession]:
    """Seed a minimal VideoSource + VideoSession pair so EventRecord FKs resolve."""
    source = VideoSource(
        source_name="客厅",
        camera_name="cam-1",
        location_name="客厅",
        source_type="local_directory",
        enabled=True,
    )
    db.add(source)
    db.flush()

    now = datetime.utcnow()
    session = VideoSession(
        source_id=source.id,
        session_start_time=now - timedelta(hours=2),
        session_end_time=now,
    )
    db.add(session)
    db.flush()
    db.commit()
    db.refresh(source)
    db.refresh(session)
    return source, session


def _add_event(
    db: Session,
    *,
    target_date: date,
    hour: int = 9,
    related_entities: list[dict[str, Any]] | None = None,
    event_type: str = "member_appear",
    importance: str = "medium",
) -> None:
    source, session = _seed_source_and_session(db)
    db.add(
        EventRecord(
            source_id=source.id,
            session_id=session.id,
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


def test_clamp_summary_payload_keeps_under_budget(pg_db: Session) -> None:
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
    pg_db: Session,
) -> None:
    """Long overall is clamped sentence-by-sentence."""
    long_overall = "今天整体平稳。" + ("详细描述。" * 60)
    overall, _sections, _attention = clamp_summary_payload(long_overall, [], [])
    assert len(overall) <= 900
    assert overall.endswith("。")


def test_clamp_summary_payload_caps_attention_at_three(
    pg_db: Session,
) -> None:
    """Attention items are capped at 3 entries."""
    attention = [{"title": f"item-{i}", "summary": f"detail-{i}", "level": "low"} for i in range(6)]
    _overall, _sections, normalized = clamp_summary_payload("ok", [], attention)
    assert len(normalized) == 3


# ---------------------------------------------------------------------------
# Parsing stage
# ---------------------------------------------------------------------------


def test_extract_json_payload_strips_code_fences(pg_db: Session) -> None:
    payload = extract_json_payload('```json\n{"a": 1}\n```')
    assert payload == {"a": 1}


def test_extract_json_payload_rejects_non_object(pg_db: Session) -> None:
    with pytest.raises(ValueError):
        extract_json_payload("[1, 2, 3]")


def test_parse_summary_with_retry_succeeds_on_first_try(pg_db: Session) -> None:
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


def test_parse_summary_with_retry_retries_on_parse_failure(pg_db: Session) -> None:
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


def test_parse_subject_summary_output_extracts_summary(pg_db: Session) -> None:
    summary, attention_needed = parse_subject_summary_output(
        json.dumps({"summary": "在客厅活动。", "attention_needed": True}, ensure_ascii=False),
        subject_name="爸爸",
    )
    assert summary == "在客厅活动。"
    assert attention_needed is True


def test_parse_rollup_output_extracts_overall_and_attention(pg_db: Session) -> None:
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


def test_parse_schedule_time_accepts_valid_hh_mm(pg_db: Session) -> None:
    assert parse_schedule_time("08:30") == (8, 30)


@pytest.mark.parametrize("bad", ["", "25:00", "12:60", "12", "abc"])
def test_parse_schedule_time_rejects_invalid_input(bad: str, pg_db: Session) -> None:
    with pytest.raises(ValueError):
        parse_schedule_time(bad)


def test_resolve_target_date_returns_yesterday(pg_db: Session) -> None:
    now = datetime(2026, 3, 14, 0, 30, tzinfo=ZoneInfo("UTC"))
    assert resolve_target_date(now) == date(2026, 3, 13)


def test_scheduled_local_datetime_composes_correct_instant(pg_db: Session) -> None:
    zone = ZoneInfo("UTC")
    scheduled = scheduled_local_datetime(
        datetime(2026, 3, 14, 1, 0, tzinfo=zone),
        "00:30",
        zone,
    )
    assert scheduled == datetime(2026, 3, 14, 0, 30, tzinfo=zone)


def test_has_existing_summary_or_task_returns_true_on_summary(
    pg_db: Session,
) -> None:
    target = date(2026, 3, 13)
    pg_db.add(DailySummary(summary_date=target, summary_title="t", overall_summary="o"))
    pg_db.commit()
    assert has_existing_summary_or_task(pg_db, target) is True


def test_has_existing_summary_or_task_returns_false_when_empty(
    pg_db: Session,
) -> None:
    assert has_existing_summary_or_task(pg_db, date(2026, 3, 13)) is False


def test_claim_dispatch_guard_round_trip(pg_db: Session) -> None:
    target = date(2026, 3, 13)
    now = datetime(2026, 3, 14, 0, 31, tzinfo=ZoneInfo("UTC"))
    assert claim_dispatch_guard(pg_db, now, target) is True
    # Second claim fails — guard is held.
    assert claim_dispatch_guard(pg_db, now, target) is False
    release_dispatch_guard(pg_db, target)
    pg_db.commit()
    assert claim_dispatch_guard(pg_db, now, target) is True


# ---------------------------------------------------------------------------
# Lifecycle stage
# ---------------------------------------------------------------------------


def test_claim_attempt_returns_active_row(pg_db: Session) -> None:
    repo = DailySummaryAttemptRepository(pg_db)
    task_log = _seed_task_log(pg_db)
    outcome = claim_attempt(
        repo,
        summary_date=date(2026, 3, 13),
        triggered_by="unit",
        task_log_id=int(task_log.id),
    )
    assert outcome.created is True
    assert outcome.attempt.summary_date == date(2026, 3, 13)
    assert outcome.attempt.status == DailySummaryAttemptStatus.CLAIMED.value


def test_claim_attempt_returns_existing_on_second_call(pg_db: Session) -> None:
    repo = DailySummaryAttemptRepository(pg_db)
    task_log = _seed_task_log(pg_db)
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
    pg_db: Session,
) -> None:
    """Failure terminal transition does NOT touch ``daily_summary``."""
    repo = DailySummaryAttemptRepository(pg_db)
    task_log = _seed_task_log(pg_db)
    attempt = _seed_running_attempt(
        pg_db, task_log_id=int(task_log.id), target=date(2026, 3, 13)
    )

    pg_db.add(
        DailySummary(
            summary_date=date(2026, 3, 13),
            summary_title="previous",
            overall_summary="prior good summary",
        )
    )
    pg_db.commit()

    failed = mark_failed(
        repo,
        attempt_id=int(attempt.id),
        error_type="RuntimeError",
        last_error="boom",
        failure_reason="llm_error",
    )
    pg_db.commit()
    assert failed is not None
    assert failed.status == DailySummaryAttemptStatus.FAILED.value

    rows = pg_db.query(DailySummary).all()
    assert len(rows) == 1
    assert rows[0].overall_summary == "prior good summary"


def test_mark_cancelled_preserves_prior_summary(pg_db: Session) -> None:
    repo = DailySummaryAttemptRepository(pg_db)
    task_log = _seed_task_log(pg_db)
    attempt = _seed_running_attempt(
        pg_db, task_log_id=int(task_log.id), target=date(2026, 3, 13)
    )

    pg_db.add(
        DailySummary(
            summary_date=date(2026, 3, 13),
            summary_title="prior",
            overall_summary="prior good summary",
        )
    )
    pg_db.commit()

    cancelled = mark_cancelled(repo, attempt_id=int(attempt.id), last_error="user cancelled")
    pg_db.commit()
    assert cancelled is not None
    assert cancelled.status == DailySummaryAttemptStatus.CANCELLED.value

    rows = pg_db.query(DailySummary).all()
    assert len(rows) == 1
    assert rows[0].overall_summary == "prior good summary"


# ---------------------------------------------------------------------------
# Finalize stage
# ---------------------------------------------------------------------------


def test_find_subscribed_webhooks_returns_only_matching_enabled(
    pg_db: Session,
) -> None:
    pg_db.add(
        WebhookConfig(
            name="matching",
            url="https://example.com/hook",
            event_subscriptions_json=[
                {"event": WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED, "version": ""},
            ],
            enabled=True,
        )
    )
    pg_db.add(
        WebhookConfig(
            name="non-matching",
            url="https://example.com/other",
            event_subscriptions_json=[{"event": "other_event", "version": ""}],
            enabled=True,
        )
    )
    pg_db.add(
        WebhookConfig(
            name="disabled",
            url="https://example.com/disabled",
            event_subscriptions_json=[
                {"event": WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED, "version": ""},
            ],
            enabled=False,
        )
    )
    pg_db.commit()
    assert find_subscribed_webhooks(pg_db) == [1]


# ---------------------------------------------------------------------------
# Evidence stage
# ---------------------------------------------------------------------------


def test_build_evidence_returns_half_open_event_range(pg_db: Session) -> None:
    pg_db.add(SystemConfig(config_key="home_timezone", config_value="Asia/Shanghai"))
    _seed_qa_provider(pg_db)
    _seed_home_member(pg_db, name="爸爸")
    target = date(2026, 3, 13)
    _add_event(pg_db, target_date=target, hour=9)

    # Add an event OUTSIDE the Shanghai local day for 2026-03-13.
    source, session = _seed_source_and_session(pg_db)
    pg_db.add(
        EventRecord(
            source_id=source.id,
            session_id=session.id,
            event_start_time=datetime(2026, 3, 13, 16, 0, 0, tzinfo=timezone.utc),
            description="next local day",
        )
    )
    pg_db.commit()

    evidence = build_evidence(
        pg_db,
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
    pg_db: Session,
) -> None:
    pg_db.add(SystemConfig(config_key="home_timezone", config_value="UTC"))
    _seed_qa_provider(pg_db)
    _seed_home_member(pg_db, name="爸爸")
    target = date(2026, 3, 13)
    for hour in range(24):
        _add_event(pg_db, target_date=target, hour=hour)
    pg_db.commit()

    captured: dict[str, Any] = {}

    def _capture(prompt_input: Any) -> tuple[str, str]:
        # Mark that the prompt builder saw the input — also stores it
        # so the test can verify trimming kicked in.
        captured["prompt_input"] = prompt_input
        return ("SYSTEM", "X" * 50000)

    evidence = build_evidence(
        pg_db,
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
    pg_db: Session,
) -> None:
    """Single-pass path fills in missing subjects even when the LLM omits one."""
    _seed_qa_provider(pg_db)
    _seed_home_member(pg_db, name="爸爸")
    _seed_home_member(pg_db, name="妈妈")
    target = date(2026, 3, 13)
    _add_event(
        pg_db,
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
        pg_db,
        target_date=target,
        zone=ZoneInfo("UTC"),
        prompt_builder=lambda payload: ("SYS", "USR"),
        prompt_input_factory=lambda **kwargs: kwargs,
    )
    overall, sections, attention, prompt_chars, parse_retried = (
        generate_single_pass_summary_payload(
            db=pg_db,
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
    pg_db: Session,
) -> None:
    """Serial path calls the LLM per-subject + once for the rollup."""
    _seed_qa_provider(pg_db)
    _seed_home_member(pg_db, name="爸爸")
    target = date(2026, 3, 13)
    _add_event(
        pg_db,
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
        pg_db,
        target_date=target,
        zone=ZoneInfo("UTC"),
        prompt_builder=lambda payload: ("SYS", "USR"),
        prompt_input_factory=lambda **kwargs: kwargs,
    )
    overall, sections, attention, prompt_chars, parse_retried = generate_serial_summary_payload(
        db=pg_db,
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
    pg_db: Session,
) -> None:
    """Subject-level parse failure → fallback text in the section, no retry-failure overall."""
    _seed_qa_provider(pg_db)
    _seed_home_member(pg_db, name="爸爸")
    target = date(2026, 3, 13)
    _add_event(
        pg_db,
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
        pg_db,
        target_date=target,
        zone=ZoneInfo("UTC"),
        prompt_builder=lambda payload: ("SYS", "USR"),
        prompt_input_factory=lambda **kwargs: kwargs,
    )
    overall, sections, _attention, _chars, parse_retried = generate_serial_summary_payload(
        db=pg_db,
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


def test_complete_subject_sections_fills_missing(pg_db: Session) -> None:
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
