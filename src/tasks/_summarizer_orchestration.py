"""Daily-summary orchestration — the generation pipeline body.

The Celery task in :mod:`src.tasks.summarizer` is a thin wrapper
that opens a :func:`task_db_session` and delegates the entire
orchestration (dispatch guard / claim + evidence / single-or-serial
LLM phase / publish + finalize) to this module so the task entry
stays a ~100-line delegate.

Patchable seam
==============

The names the existing tests monkeypatch at
``src.tasks.summarizer.<name>`` (``_get_pipeline_orchestrator``,
``home_now`` and ``SERIAL_SPLIT_PROMPT_THRESHOLD``) are read
dynamically through the task module via :func:`_seam` at call
time, so a ``monkeypatch.setattr("src.tasks.summarizer.X", ...)``
in an existing test takes effect unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional, cast

from sqlalchemy.orm import Session

from src.application.pipeline.commands import (
    GenerateDailySummaryCommand,
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
from src.core.i18n import get_system_default_locale
from src.core.i18n.locale_directive import get_summary_title
from src.models.task_log import TaskLog
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.pipeline_constants import TaskStatus
from src.services.provider_key_crypto import decrypt_provider_api_key
from src.services.provider_selector import (
    PROVIDER_TYPE_QA,
    find_required_enabled_provider,
)
from src.services.summarizer import (
    attempt_already_running,
    build_evidence,
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
    ensure_task_not_cancelled,
    finalize_task_log,
)

logger = logging.getLogger(__name__)


def _seam() -> Any:
    """Return :mod:`src.tasks.summarizer` (the patchable seam module)."""
    import src.tasks.summarizer as _summarizer_module

    return _summarizer_module


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


def _close_gateway_safely(client: Any) -> None:
    """Close ``client`` if it implements ``close()``.

    Mirrors the analyzer's pattern. A bare test stub may not
    implement ``close``; calling :func:`getattr` keeps the
    orchestrator robust to misbehaving fakes.
    """
    close = getattr(client, "close", None)
    if close is None:
        return
    close()


def _get_pipeline_orchestrator() -> PipelineOrchestrator:
    """Build a fresh :class:`PipelineOrchestrator`.

    Read through the patchable seam so the legacy
    ``monkeypatch.setattr("src.tasks.summarizer._get_pipeline_orchestrator",
    ...)`` tests keep landing.
    """
    return cast(PipelineOrchestrator, _seam()._get_pipeline_orchestrator())


def run_dispatch_scheduled(db: Session, *, container: Any, now: datetime) -> dict[str, Any]:
    """Resolve dispatch eligibility and enqueue when due.

    Thin entry point mirroring the legacy
    ``dispatch_scheduled_daily_summary_task`` contract.
    """
    return _dispatch_scheduled(db, container=container, now=now)


def _dispatch_scheduled(
    db: Any,
    *,
    container: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Resolve the dispatch eligibility; enqueue when due."""
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


def run_daily_summary_generation(
    db: Session,
    *,
    target_date: date,
    queue_task_id: Optional[str],
    container: Any,
    task_log: TaskLog,
) -> dict[str, Any]:
    """Drive the full single-day generation flow and serialise the outcome.

    Thin entry point for the ``generate_daily_summary_task`` Celery
    wrapper. The wrapper is responsible for opening the session,
    binding the ``TaskLog`` and the top-level commit / cancellation
    exception envelope; this function owns the pipeline and returns
    the legacy response payload via :func:`_outcome_to_response`.
    """
    outcome = _run_generation_pipeline(
        db=db,
        target_date=target_date,
        queue_task_id=queue_task_id,
        container=container,
        task_log=task_log,
    )
    return _outcome_to_response(outcome)


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

    ``serial_split_prompt_threshold`` defaults to the seam's module
    ``SERIAL_SPLIT_PROMPT_THRESHOLD`` (read dynamically through
    :func:`_seam`) so the legacy
    ``monkeypatch.setattr("src.tasks.summarizer.SERIAL_SPLIT_PROMPT_THRESHOLD",
    ...)`` test keeps working.
    """
    if locale is None:
        locale = get_system_default_locale()

    if serial_split_prompt_threshold is None:
        serial_split_prompt_threshold = int(_seam().SERIAL_SPLIT_PROMPT_THRESHOLD)

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
