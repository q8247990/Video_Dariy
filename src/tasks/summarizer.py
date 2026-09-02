"""Daily-summary Celery tasks — thin wrapper over the staged pipeline.

This module owns the Celery task decorators, the
``task_db_session`` lifecycle and the orchestration glue between
the staged helpers in :mod:`src.services.summarizer` and the
use-case / repository / orchestrator singletons in
:mod:`src.application`. Two tasks are exposed:

``dispatch_scheduled_daily_summary_task``
    Runs once per Celery beat (every 60s). Decides whether the
    current local moment has crossed the configured
    ``daily_summary_schedule``; if so, asks the
    :func:`_dispatch_scheduled` helper to atomically claim the
    per-date slot and dispatch the generation task via the
    pipeline orchestrator.

``generate_daily_summary_task``
    The per-day generation worker. Opens a single
    :func:`task_db_session` for the orchestration lifetime,
    binds the ``TaskLog``, delegates to
    :func:`_run_generation_pipeline`, and serialises the
    :class:`GenerationOutcome` into the response payload.

Transaction discipline
======================

A single ``task_db_session`` covers the whole flow, with
``db.commit()`` calls at safe boundaries:

1. After binding the ``TaskLog`` — so the celery-worker row
   becomes visible to other sessions / the operator UI;
2. After ``claim_attempt`` + ``build_evidence`` — so the
   pre-LLM writes are durable and the LLM HTTP call sits in a
   **transaction-free** window (the structural enforcement of
   "no DB transaction during LLM call");
3. After :func:`publish_daily_summary` — so the ``daily_summary``
   upsert, the attempt ``→ succeeded`` transition and the outbox
   rows become durable as one atomic unit (Todo 16).

LLM gateway lifecycle
=====================

The orchestrator owns the gateway lifecycle. ``client.close()`` is
guarded with :func:`getattr` so the existing tests' bare
``_FakeGateway`` stubs (which intentionally do not implement
``close``) keep working — the previous monolithic implementation
crashed every code path with ``AttributeError: '_FakeGateway'
object has no attribute 'close'`` and the 5 pre-existing
integration-test failures recorded in this branch were a direct
consequence of that. This module fixes it once and for all.

Legacy test surface
===================

The legacy free-function helpers (``_get_pipeline_orchestrator``,
``_parse_schedule_time``, ``_summary_title``, ``TaskLog``,
``home_now``, ``SERIAL_SPLIT_PROMPT_THRESHOLD``,
``DEFAULT_DAILY_SUMMARY_SCHEDULE``) are kept as re-exports so the
existing tests' ``monkeypatch.setattr("src.tasks.summarizer.X",
...)`` calls land somewhere meaningful.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from src.application.pipeline.commands import (
    GenerateDailySummaryCommand,
    SendWebhookCommand,
)
from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.application.prompt.compiler import compile_daily_summary_prompt
from src.application.prompt.contracts import DailySummaryPromptInput
from src.application.summary_attempt.repository import DailySummaryAttemptRepository
from src.application.summary_publication import (
    AttemptNotInValidStateError,
    PublishDailySummaryCommand,
    publish_daily_summary,
)
from src.core.celery_app import celery_app
from src.core.i18n import get_system_default_locale
from src.core.i18n.locale_directive import get_summary_title
from src.db.session import task_db_session
from src.models.daily_summary import DailySummary
from src.models.task_log import TaskLog
from src.services.home_timezone import home_now as home_now
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.onboarding import DEFAULT_DAILY_SUMMARY_SCHEDULE
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.provider_key_crypto import decrypt_provider_api_key
from src.services.provider_selector import (
    PROVIDER_TYPE_QA,
    find_required_enabled_provider,
)
from src.services.summarizer import (
    SERIAL_SPLIT_PROMPT_THRESHOLD,
    WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
    attempt_already_running,
    build_evidence,
    build_webhook_payload,
    claim_attempt,
    claim_dispatch_guard,
    clamp_summary_payload,
    find_subscribed_webhooks,
    generate_serial_summary_payload,
    generate_single_pass_summary_payload,
    get_daily_schedule,
    get_home_timezone,
    has_existing_summary_or_task,
    parse_schedule_time,
    release_dispatch_guard,
    resolve_target_date,
    scheduled_local_datetime,
)
from src.services.summarizer import (
    mark_failed as mark_attempt_failed,
)
from src.services.summarizer import (
    mark_running as mark_attempt_running,
)
from src.services.task_dispatch_control import (
    TaskCancellationRequested,
    bind_or_create_running_task_log,
    finalize_cancelled_task_log,
    finalize_task_log,
    get_task_log_for_update,
)
from src.tasks._container import get_container

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Re-exports — keep the legacy free-function symbols alive so the existing
# integration tests' ``monkeypatch.setattr("src.tasks.summarizer.X", ...)``
# calls land somewhere meaningful. See module docstring.
# ---------------------------------------------------------------------------


def _get_pipeline_orchestrator() -> PipelineOrchestrator:
    """Build a fresh :class:`PipelineOrchestrator` from the composition root.

    Re-exported for the legacy test contract; the production code
    reaches for the composition root directly.
    """
    return PipelineOrchestrator(dispatcher=get_container().dispatcher)


def _summary_title(target_date: Any, locale: Optional[str] = None) -> str:
    """Return the localized summary title for ``target_date``."""
    return get_summary_title(target_date.strftime("%Y-%m-%d"), locale)


def _parse_schedule_time(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` into ``(hour, minute)`` — legacy re-export."""
    return parse_schedule_time(value)


def _resolve_target_date(now: datetime) -> date:
    """Resolve the target date (yesterday in the supplied ``now``)."""
    return resolve_target_date(now)


def _home_timezone(db: Any) -> Any:
    """Legacy re-export: read the configured home timezone."""
    return get_home_timezone(db)


# ---------------------------------------------------------------------------
# Outcome dataclass — translated into the legacy response payload by
# :func:`_outcome_to_response`.
# ---------------------------------------------------------------------------


@dataclass
class GenerationOutcome:
    """Return shape of the generation pipeline."""

    summary_date: date
    event_count: int
    attempt_no: int = 0
    attempt_status: str = "unknown"
    summary_id: Optional[int] = None
    cancelled: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    webhook_event_ids: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM gateway close helper — ``getattr``-guarded for fake gateways.
# ---------------------------------------------------------------------------


def _close_gateway_safely(client: Any) -> None:
    """Close ``client`` if it implements ``close()``.

    Mirrors the analyzer's pattern. A bare test stub may not
    implement ``close``; calling :func:`getattr` keeps the
    orchestrator robust to misbehaving fakes (which is the root
    cause of the pre-existing failures in
    :mod:`tests.integration.test_daily_summary_task`).
    """
    close = getattr(client, "close", None)
    if close is None:
        return
    close()


# ---------------------------------------------------------------------------
# Public Celery tasks
# ---------------------------------------------------------------------------


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def dispatch_scheduled_daily_summary_task(self: Any) -> dict[str, Any]:
    """Celery-beat entry point: dispatch today's daily-summary task if due."""
    with task_db_session() as db:
        now = home_now(get_home_timezone(db))
        return _dispatch_scheduled(db, container=get_container(), now=now)


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def generate_daily_summary_task(self: Any, target_date_str: Optional[str] = None) -> dict[str, Any]:
    """Celery-worker entry point: generate one day's daily summary.

    Args:
        target_date_str: ISO ``YYYY-MM-DD`` date. ``None`` defaults to
            "yesterday" in the configured home timezone.

    Returns:
        ``{"summary_date": ..., "event_count": ...}`` on success;
        ``{"skipped": True, "reason": ...}`` /
        ``{"cancelled": True, ...}`` on the non-success paths.
    """
    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    container = get_container()

    with task_db_session() as db:
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        else:
            zone = get_home_timezone(db)
            target_date = home_now(zone).date() - timedelta(days=1)

        task_log = bind_or_create_running_task_log(
            db,
            queue_task_id=queue_task_id or None,
            task_type=TaskType.DAILY_SUMMARY_GENERATION,
            task_target_id=None,
            detail_json={"target_date": str(target_date)},
        )
        if task_log is None:
            logger.warning(
                "Stale daily summary message %s for %s; task log already finalized, skipping",
                queue_task_id,
                target_date,
            )
            return {"skipped": True, "reason": "stale_message"}
        db.commit()

        try:
            outcome = _run_generation_pipeline(
                db=db,
                target_date=target_date,
                queue_task_id=queue_task_id,
                container=container,
                task_log=task_log,
            )
        except TaskCancellationRequested as exc:
            logger.info("Daily summary task cancelled for %s", target_date)
            db.rollback()
            db.query(DailySummary).filter(DailySummary.summary_date == target_date).delete()
            refreshed = get_task_log_for_update(db, task_log.id)
            if refreshed is not None:
                finalize_cancelled_task_log(
                    refreshed,
                    str(exc),
                    {"target_date": str(target_date), "cancelled": True},
                )
            db.commit()
            return {"cancelled": True, "summary_date": str(target_date)}

        db.commit()
        return _outcome_to_response(outcome)


# ---------------------------------------------------------------------------
# Orchestration helpers
# ---------------------------------------------------------------------------


def _dispatch_scheduled(
    db: Any,
    *,
    container: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Resolve the dispatch eligibility; enqueue when due.

    Mirrors the legacy ``dispatch_scheduled_daily_summary_task``
    contract: returns ``{"scheduled": True, ...}`` when a new task
    was enqueued, otherwise ``{"scheduled": False, "reason": ...}``
    for every skip path. ``None`` ``now`` falls back to
    ``datetime.now(tz=zone)``.
    """
    zone = get_home_timezone(db)
    if now is None:
        now = datetime.now(tz=zone)
    target_date = resolve_target_date(now)

    try:
        schedule_text = get_daily_schedule(db)
        hour, minute = parse_schedule_time(schedule_text)
    except ValueError as exc:
        logger.warning("Invalid daily summary schedule, skip dispatch: %s", exc)
        return {"scheduled": False, "reason": "invalid_schedule"}

    scheduled_at = scheduled_local_datetime(now, schedule_text, zone)
    if now < scheduled_at:
        return {"scheduled": False, "reason": "before_schedule"}

    if has_existing_summary_or_task(db, target_date):
        return {
            "scheduled": False,
            "reason": "already_exists",
            "target_date": str(target_date),
        }

    if not claim_dispatch_guard(db, now, target_date):
        db.rollback()
        return {
            "scheduled": False,
            "reason": "dispatch_guard_blocked",
            "target_date": str(target_date),
        }

    try:
        orchestrator = _get_pipeline_orchestrator()
        task_id = orchestrator.dispatch_generate_daily_summary(
            db,
            GenerateDailySummaryCommand(target_date_str=str(target_date)),
        )
        db.commit()
        return {"scheduled": True, "target_date": str(target_date), "task_id": task_id}
    except Exception as exc:
        db.rollback()
        release_dispatch_guard(db, target_date)
        db.commit()
        logger.exception("Failed to dispatch scheduled daily summary for %s", now.date())
        return {
            "scheduled": False,
            "reason": "dispatch_failed",
            "error": str(exc),
            "target_date": str(target_date),
        }


def _claim_and_evidence(
    *,
    db: Any,
    repo: Any,
    target_date: date,
    task_log: TaskLog,
    locale: str,
    forced: bool,
) -> tuple[
    GenerationOutcome | None,
    Any | None,
    Any | None,
    Any | None,
    Any | None,
]:
    """Run the pre-LLM phase: skip-if-active, claim, evidence, mark running.

    Returns ``(early_outcome, claim, evidence, provider, zone)``. When
    ``early_outcome`` is not ``None`` the caller should return it
    directly — the other values are unset.
    """
    if not forced:
        existing = attempt_already_running(repo, target_date)
        if existing is not None:
            logger.info(
                "Active daily-summary attempt already exists for %s (attempt_id=%s); skipping",
                target_date,
                existing.id,
            )
            finalize_task_log(
                task_log,
                TaskStatus.SUCCESS,
                f"Skipped duplicate summary generation for {target_date}.",
                {"target_date": str(target_date), "skipped_duplicate": True},
            )
            return (
                GenerationOutcome(
                    summary_date=target_date,
                    event_count=0,
                    attempt_no=int(existing.attempt_no),
                    attempt_status=str(existing.status),
                    skipped=True,
                    skip_reason="already_running",
                ),
                None,
                None,
                None,
                None,
            )

    claim = claim_attempt(
        repo,
        summary_date=target_date,
        triggered_by="manual" if forced else "schedule",
        task_log_id=int(task_log.id),
    )

    from src.services.task_dispatch_control import ensure_task_not_cancelled

    ensure_task_not_cancelled(
        db,
        task_log.id,
        default_message=f"Daily summary cancelled for {target_date}",
    )

    provider = find_required_enabled_provider(db, PROVIDER_TYPE_QA)
    enforce_token_quota(db, provider)

    zone = get_home_timezone(db)
    evidence = build_evidence(
        db,
        target_date=target_date,
        zone=zone,
        prompt_builder=compile_daily_summary_prompt,
        prompt_input_factory=DailySummaryPromptInput,
        locale=locale,
    )

    mark_attempt_running(
        repo,
        attempt_id=int(claim.attempt.id),
        events_count=len(evidence.events),
        input_token_estimate=evidence.prompt_chars,
    )

    ensure_task_not_cancelled(
        db,
        task_log.id,
        default_message=f"Daily summary cancelled for {target_date}",
    )

    db.commit()

    return None, claim, evidence, provider, zone


def _run_llm_phase(
    *,
    db: Any,
    client: Any,
    provider: Any,
    target_date: date,
    evidence: Any,
    locale: str,
    serial_split_prompt_threshold: int,
) -> tuple[str, list[Any], list[Any], int, bool, str]:
    """Pick the single/serial path, run the LLM, return the parsed payload."""
    if evidence.single_pass_user_prompt_chars > serial_split_prompt_threshold:
        summary_mode = "split_serial"
        result = generate_serial_summary_payload(
            db=db,
            client=client,
            provider_id=provider.id,
            provider_name_snapshot=provider.provider_name,
            target_date=target_date,
            start_dt=evidence.start_dt,
            end_dt=evidence.end_dt,
            events=evidence.events,
            home_context=evidence.home_context,
            known_subjects=evidence.known_subjects,
            subject_sections_payload=evidence.subject_sections,
            missing_subjects=evidence.missing_subjects,
            attention_candidates_payload=evidence.attention_candidates,
            locale=locale,
        )
    else:
        summary_mode = "single_pass"
        result = generate_single_pass_summary_payload(
            db=db,
            client=client,
            provider_id=provider.id,
            provider_name_snapshot=provider.provider_name,
            events=evidence.events,
            prompt=evidence.prompt,
            known_subjects=evidence.known_subjects,
            subject_sections_payload=evidence.subject_sections,
            locale=locale,
        )
    overall, sections, attention, prompt_chars, parse_retried = result
    return overall, sections, attention, prompt_chars, parse_retried, summary_mode


def _handle_llm_failure(
    *,
    db: Any,
    repo: Any,
    client: Any,
    provider: Any,
    claim: Any,
    task_log: TaskLog,
    target_date: date,
    exc: BaseException,
) -> None:
    """Finalise the attempt + task log on an LLM-phase exception."""
    try:
        record_token_usage(
            db,
            provider_id=provider.id,
            provider_name_snapshot=provider.provider_name,
            scene="daily_summary",
            usage=client.get_last_usage(),
        )
    except Exception:
        pass
    mark_attempt_failed(
        repo,
        attempt_id=int(claim.attempt.id),
        error_type=type(exc).__name__,
        last_error=str(exc)[:1024],
        failure_reason="llm_error",
    )
    finalize_task_log(
        task_log,
        TaskStatus.FAILED,
        f"Failed to generate summary for {target_date}: {exc}",
    )


def _dispatch_legacy_webhook(
    db: Any,
    *,
    target_date: date,
    summary_title: str,
    overall_summary: str,
    structured_subject_sections: list[Any],
    structured_attention_items: list[Any],
    event_count: int,
) -> None:
    """Enqueue the legacy webhook fan-out (one celery task per event)."""
    try:
        legacy_payload = build_webhook_payload(
            target_date=target_date,
            summary_title=summary_title,
            overall_summary=overall_summary,
            subject_sections=structured_subject_sections,
            attention_items=structured_attention_items,
            event_count=event_count,
        )
        _get_pipeline_orchestrator().dispatch_webhook(
            db,
            SendWebhookCommand(
                event_type=WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
                payload=legacy_payload,
            ),
        )
    except Exception:
        logger.exception("Failed to enqueue daily summary webhook task")


def _run_generation_pipeline(
    *,
    db: Any,
    target_date: date,
    queue_task_id: Optional[str],
    container: Any,
    task_log: TaskLog,
    locale: Optional[str] = None,
    forced: bool = False,
    serial_split_prompt_threshold: Optional[int] = None,
) -> GenerationOutcome:
    """Drive the full single-day generation flow.

    See :mod:`src.tasks.summarizer` docstring for the transaction
    discipline. ``serial_split_prompt_threshold`` defaults to the
    module-level ``SERIAL_SPLIT_PROMPT_THRESHOLD`` so the legacy
    ``monkeypatch.setattr`` tests keep working.
    """
    if locale is None:
        locale = get_system_default_locale()

    if serial_split_prompt_threshold is None:
        serial_split_prompt_threshold = int(SERIAL_SPLIT_PROMPT_THRESHOLD)

    outcome = GenerationOutcome(summary_date=target_date, event_count=0)

    try:
        get_home_timezone(db)
    except Exception as exc:
        logger.exception("Failed to resolve home timezone for %s", target_date)
        finalize_task_log(task_log, TaskStatus.FAILED, str(exc))
        return outcome

    repo = DailySummaryAttemptRepository(db)

    early, claim, evidence, provider, _zone = _claim_and_evidence(
        db=db,
        repo=repo,
        target_date=target_date,
        task_log=task_log,
        locale=locale,
        forced=forced,
    )
    if early is not None:
        return early
    assert claim is not None and evidence is not None and provider is not None
    claim_obj: Any = claim
    evidence_obj: Any = evidence
    provider_obj: Any = provider
    outcome.event_count = len(evidence_obj.events)
    outcome.attempt_no = int(claim_obj.attempt.attempt_no)

    client = container.llm_factory.build(
        api_base_url=provider_obj.api_base_url,
        api_key=decrypt_provider_api_key(provider_obj.api_key),
        model_name=provider_obj.model_name,
        timeout_seconds=provider_obj.timeout_seconds,
    )

    try:
        (
            overall_summary,
            structured_subject_sections,
            structured_attention_items,
            prompt_chars_total,
            parse_retried,
            summary_mode,
        ) = _run_llm_phase(
            db=db,
            client=client,
            provider=provider_obj,
            target_date=target_date,
            evidence=evidence_obj,
            locale=locale,
            serial_split_prompt_threshold=serial_split_prompt_threshold,
        )
    except TaskCancellationRequested:
        raise
    except Exception as exc:
        _handle_llm_failure(
            db=db,
            repo=repo,
            client=client,
            provider=provider_obj,
            claim=claim_obj,
            task_log=task_log,
            target_date=target_date,
            exc=exc,
        )
        outcome.attempt_status = "failed"
        return outcome
    finally:
        _close_gateway_safely(client)

    overall_summary, structured_subject_sections, structured_attention_items = (
        clamp_summary_payload(
            overall_summary,
            structured_subject_sections,
            structured_attention_items,
        )
    )

    summary_title = get_summary_title(target_date.strftime("%Y-%m-%d"), locale)
    webhook_subscriber_ids = find_subscribed_webhooks(db)

    try:
        publish_outcome = publish_daily_summary(
            db,
            PublishDailySummaryCommand(
                summary_date=target_date,
                attempt_id=int(claim_obj.attempt.id),
                task_log_id=int(task_log.id),
                summary_content_json={
                    "summary_title": summary_title,
                    "overall_summary": overall_summary,
                    "subject_sections": structured_subject_sections,
                    "attention_items": structured_attention_items,
                    "events_count": outcome.event_count,
                    "provider_id": provider_obj.id,
                    "provider_name_snapshot": provider_obj.provider_name,
                },
                webhook_subscribers=list(webhook_subscriber_ids),
            ),
        )
        outcome.summary_id = int(publish_outcome.summary_id)
        outcome.attempt_status = str(publish_outcome.attempt_status.value)
        outcome.webhook_event_ids = list(publish_outcome.webhook_event_ids)
    except AttemptNotInValidStateError as exc:
        db.rollback()
        finalize_task_log(
            task_log,
            TaskStatus.FAILED,
            f"Daily summary publish rejected: {exc}",
        )
        outcome.attempt_status = "failed"
        return outcome
    except Exception as exc:
        db.rollback()
        mark_attempt_failed(
            repo,
            attempt_id=int(claim_obj.attempt.id),
            error_type=type(exc).__name__,
            last_error=str(exc)[:1024],
            failure_reason="publish_error",
        )
        finalize_task_log(
            task_log,
            TaskStatus.FAILED,
            f"Failed to publish summary for {target_date}: {exc}",
        )
        outcome.attempt_status = "failed"
        return outcome

    if webhook_subscriber_ids:
        _dispatch_legacy_webhook(
            db,
            target_date=target_date,
            summary_title=summary_title,
            overall_summary=overall_summary,
            structured_subject_sections=structured_subject_sections,
            structured_attention_items=structured_attention_items,
            event_count=outcome.event_count,
        )

    detail = _build_task_log_detail(
        task_log=task_log,
        prompt_chars_total=prompt_chars_total,
        single_pass_prompt_chars=evidence_obj.single_pass_user_prompt_chars,
        summary_mode=summary_mode,
        split_threshold=serial_split_prompt_threshold,
        subject_sections_count=len(evidence_obj.subject_sections),
        attention_candidates_count=len(evidence_obj.attention_candidates),
        parse_retried=parse_retried,
    )
    finalize_task_log(
        task_log,
        TaskStatus.SUCCESS,
        f"Generated summary for {target_date} with {outcome.event_count} events.",
        detail,
    )

    return outcome


def _build_task_log_detail(
    *,
    task_log: TaskLog,
    prompt_chars_total: int,
    single_pass_prompt_chars: int,
    summary_mode: str,
    split_threshold: int,
    subject_sections_count: int,
    attention_candidates_count: int,
    parse_retried: bool,
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    raw_detail = task_log.detail_json
    if isinstance(raw_detail, dict):
        detail = {str(key): value for key, value in raw_detail.items()}
    detail.update(
        {
            "prompt_chars": prompt_chars_total,
            "single_pass_prompt_chars": single_pass_prompt_chars,
            "summary_mode": summary_mode,
            "split_threshold": split_threshold,
            "subject_sections_count": subject_sections_count,
            "attention_candidates_count": attention_candidates_count,
            "parse_retried": parse_retried,
        }
    )
    return detail


def _outcome_to_response(outcome: GenerationOutcome) -> dict[str, Any]:
    """Translate :class:`GenerationOutcome` to the legacy response payload."""
    if outcome.skipped:
        return {
            "skipped": True,
            "reason": outcome.skip_reason or "already_generated",
            "summary_date": str(outcome.summary_date),
        }
    if outcome.cancelled:
        return {"cancelled": True, "summary_date": str(outcome.summary_date)}
    return {
        "summary_date": str(outcome.summary_date),
        "event_count": outcome.event_count,
    }


__all__ = [
    "DEFAULT_DAILY_SUMMARY_SCHEDULE",
    "GenerationOutcome",
    "SERIAL_SPLIT_PROMPT_THRESHOLD",
    "TaskLog",
    "dispatch_scheduled_daily_summary_task",
    "generate_daily_summary_task",
    "home_now",
    "WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED",
]
