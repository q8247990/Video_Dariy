"""Evidence preparation — produce the inputs for the LLM prompts.

The :class:`Evidence` dataclass is the single object the generation
stage consumes. Building it is the only DB read the generation
stage needs, and :func:`build_evidence` is a small composition of
existing services (``home_profile`` / ``home_timezone`` /
``daily_summary.preprocess``) so the orchestration stays a pure
dispatcher.

The prompt-builder is injected so this module stays free of
:mod:`src.application.*` imports. The Celery task supplies the
:func:`src.application.prompt.compiler.compile_daily_summary_prompt`
callable (or a stub in tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.models.event_record import EventRecord
from src.schemas.home_profile import coerce_focus_points
from src.services.attention import attention_focus_keys
from src.services.daily_summary.preprocess import (
    build_known_subjects,
    build_subject_event_mapping,
    extract_attention_candidates,
)
from src.services.home_profile import build_home_context
from src.services.home_timezone import local_day_bounds

PromptBuilder = Callable[..., tuple[str, str]]


@dataclass(frozen=True)
class Evidence:
    """All inputs the generation stage needs to produce a summary.

    Attributes:
        target_date: The summary date (timezone-naive ``date``).
        start_dt / end_dt: UTC half-open range of the day's events.
        home_context: Snapshot of ``build_home_context(db)`` —
            home profile, members and pets.
        known_subjects: Compact ``[{"subject_name", "subject_type"}]``
            list extracted from the home context.
        subject_sections: Per-subject roll-up of events.
        missing_subjects: Known subjects with zero events.
        attention_candidates: Operator-facing attention candidates.
        events: Ordered list of the day's ``EventRecord`` rows.
        prompt: The ``(system, user)`` prompt tuple.
        prompt_chars: ``len(prompt[1])`` — convenient for the
            single-vs-serial decision.
        locale: Locale directive passed to the prompt builder.
        single_pass_user_prompt_chars: ``len(prompt[1])`` —
            duplicated for the orchestrator's split-decision logic.
    """

    target_date: date
    start_dt: datetime
    end_dt: datetime
    home_context: dict[str, Any]
    known_subjects: list[dict[str, str]]
    subject_sections: list[dict[str, Any]]
    missing_subjects: list[str]
    attention_candidates: list[dict[str, Any]]
    events: list[EventRecord]
    prompt: tuple[str, str]
    prompt_chars: int
    locale: Optional[str] = None
    single_pass_user_prompt_chars: int = 0


def build_evidence(
    db: Session,
    *,
    target_date: date,
    zone: ZoneInfo,
    prompt_builder: PromptBuilder,
    prompt_input_factory: Callable[..., Any],
    locale: Optional[str] = None,
) -> Evidence:
    """Build an :class:`Evidence` for ``target_date`` using ``zone``.

    Args:
        db: Caller-owned ``Session``; the helper runs one events
            query and reads the home profile. The caller owns the
            commit / rollback.
        target_date: The date the summary covers.
        zone: Home timezone (``ZoneInfo`` instance).
        prompt_builder: Callable that builds the daily-summary
            prompt. Signature ``(prompt_input) -> (system, user)``.
            The production wiring supplies
            :func:`src.application.prompt.compiler.compile_daily_summary_prompt`.
        prompt_input_factory: Callable that builds the
            ``DailySummaryPromptInput`` payload from the evidence
            fields. The production wiring supplies
            :class:`src.application.prompt.contracts.DailySummaryPromptInput`.
            Accepting the factory as a parameter keeps
            :mod:`src.services.summarizer` free of
            :mod:`src.application.*` imports.
        locale: Optional locale directive.
    """
    start_dt, end_dt = local_day_bounds(zone, target_date)
    events = (
        db.query(EventRecord)
        .filter(
            EventRecord.event_start_time >= start_dt,
            EventRecord.event_start_time < end_dt,
        )
        .order_by(EventRecord.event_start_time.asc())
        .all()
    )

    home_context = build_home_context(db)
    known_subjects = build_known_subjects(home_context)
    subject_sections, missing_subjects, _ = build_subject_event_mapping(events, known_subjects)
    attention_keys = attention_focus_keys(
        coerce_focus_points(home_context["home_profile"].get("focus_items"))
    )
    attention_candidates = extract_attention_candidates(events, attention_keys=attention_keys)

    subject_sections_payload = [item.model_dump() for item in subject_sections]
    attention_candidates_payload = [item.model_dump() for item in attention_candidates]

    prompt_input = prompt_input_factory(
        home_context=home_context,
        summary_date=target_date,
        time_range_start=start_dt.isoformat(),
        time_range_end=end_dt.isoformat(),
        subject_sections=subject_sections_payload,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates_payload,
        locale=locale,
    )
    prompt = prompt_builder(prompt_input)

    return Evidence(
        target_date=target_date,
        start_dt=start_dt,
        end_dt=end_dt,
        home_context=home_context,
        known_subjects=known_subjects,
        subject_sections=subject_sections_payload,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates_payload,
        events=events,
        prompt=prompt,
        prompt_chars=len(prompt[1]),
        locale=locale,
        single_pass_user_prompt_chars=len(prompt[1]),
    )


__all__ = ["Evidence", "PromptBuilder", "build_evidence"]
