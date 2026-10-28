"""Session-build orchestration and sealing policy."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.pipeline_constants import AnalysisPriority, ScanMode, SessionAnalysisStatus
from src.models.video_session import VideoSession
from src.services.pipeline_state import transition_session
from src.services.session_build import discovery, persistence
from src.services.session_build.reducer import reduce_files
from src.services.session_build.types import (
    SEAL_BUFFER_SECONDS,
    InsertedFile,
    SealedSessionInfo,
    SessionBuildResult,
)

# --- seal_policy.py ---
logger = logging.getLogger(__name__)


# --- seal_policy.py ---
@dataclass(frozen=True)
class SealDecision:
    """Pure seal-policy output.

    :attr:`sealed_session_ids` is the list of
    :class:`VideoSession.id` values the runner must transition
    ``OPEN → SEALED``. The order matches the input list order so
    the persistence layer iterates deterministically.
    """

    sealed_session_ids: list[int] = field(default_factory=list)


# --- seal_policy.py ---
def _as_aware_utc(value: datetime) -> datetime:
    """Coerce any ``datetime`` (naive or aware) to aware UTC.

    The legacy callers in :mod:`src.tasks.session_build` pass
    naive camera times as ``now_utc``; the session rows the
    pure helper reads come back from PG with ``tzinfo=UTC``
    attached. The comparison in
    :func:`select_sessions_to_seal` needs the two values to
    share a tzinfo; we coerce naive values to UTC explicitly.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --- seal_policy.py ---
def _select_open_sessions_in_order(
    sessions: list[VideoSession], *, scan_mode: str
) -> list[VideoSession]:
    if scan_mode == ScanMode.FULL:
        return sorted(sessions, key=lambda s: _as_aware_utc(s.session_start_time))
    return sorted(sessions, key=lambda s: _as_aware_utc(s.session_end_time))


# --- seal_policy.py ---
def _buffer_eligible_ids(sessions: list[VideoSession], *, now_utc: datetime) -> list[int]:
    """Return the ids of sessions whose seal buffer has elapsed."""
    cutoff = _as_aware_utc(now_utc) - timedelta(seconds=SEAL_BUFFER_SECONDS)
    return [session.id for session in sessions if _as_aware_utc(session.session_end_time) <= cutoff]


# --- seal_policy.py ---
def select_sessions_to_seal(
    *,
    scan_mode: str,
    open_sessions: list[VideoSession],
    now_utc: datetime | None = None,
) -> SealDecision:
    """Return the ids of sessions the seal policy says to seal.

    Args:
        scan_mode: :class:`ScanMode.HOT` or
            :class:`ScanMode.FULL`.
        open_sessions: The snapshot of still-``OPEN`` sessions
            for the source. The caller is expected to filter
            the runner's source-id-scoped query; this function
            does not re-filter, but it sorts by
            ``session_end_time`` for ``ScanMode.HOT`` (latest
            last) and by ``session_start_time`` for
            ``ScanMode.FULL``.
        now_utc: Override for the current time. ``None`` uses
            :func:`datetime.now(timezone.utc)`. Tests pass an
            explicit instant to keep the buffer-based seal
            stable.

    Returns:
        A :class:`SealDecision`; ``sealed_session_ids`` is the
        ``OPEN`` sessions to transition. The order matches the
        input ordering; the persistence layer iterates in the
        same order so the audit log is deterministic.
    """
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    sorted_open = _select_open_sessions_in_order(open_sessions, scan_mode=scan_mode)

    if scan_mode == ScanMode.FULL:
        return SealDecision(sealed_session_ids=[session.id for session in sorted_open])

    if not sorted_open:
        return SealDecision(sealed_session_ids=[])

    # HOT mode: every OPEN session except the latest can be
    # sealed (a fresh OPEN session confirms the previous one is
    # complete). The latest may then be sealed too once the
    # seal buffer has elapsed without any new file.
    if len(sorted_open) <= 1:
        return SealDecision(sealed_session_ids=_buffer_eligible_ids(sorted_open, now_utc=now))

    ids = [session.id for session in sorted_open[:-1]]
    latest = sorted_open[-1]
    if _as_aware_utc(latest.session_end_time) + timedelta(
        seconds=SEAL_BUFFER_SECONDS
    ) <= _as_aware_utc(now):
        ids.append(latest.id)
    return SealDecision(sealed_session_ids=ids)


# --- seal_policy.py ---
def apply_seal_transitions(
    db: Session,
    *,
    source_id: int,
    scan_mode: str,
    decision: SealDecision,
    open_sessions: list[VideoSession],
    priority: str,
) -> list[SealedSessionInfo]:
    """Issue the ``OPEN → SEALED`` transitions; return the envelope list.

    The function iterates :attr:`decision.sealed_session_ids`
    in order, locates each session in the in-memory
    ``open_sessions`` snapshot and writes the transition
    through
    :func:`src.services.pipeline_state.transition_session`.

    Args:
        db: Caller-owned SQLAlchemy session.
        source_id: Source-scoped identifier the
            :class:`SealedSessionInfo` carries for the analyzer
            dispatcher.
        scan_mode: :class:`ScanMode.HOT` or
            :class:`ScanMode.FULL`; forwarded to the transition
            reason / source strings so the audit log can
            attribute the seal to the right code path.
        decision: The pure output from
            :func:`select_sessions_to_seal`.
        open_sessions: The same in-memory snapshot the pure
            half saw; used to find the per-id
            :class:`VideoSession` to transition.
        priority: analysis-priority value the runner
            inherited from :attr:`scan_mode`. The value lands
            on ``VideoSession.analysis_priority`` and is
            mirrored into the analyzer dispatch envelope.

    Returns:
        The :class:`SealedSessionInfo` list the slim Celery
        task iterates when dispatching analyzers.
    """
    sealed_envelope: list[SealedSessionInfo] = []
    if not decision.sealed_session_ids:
        return sealed_envelope

    by_id: dict[int, VideoSession] = {session.id: session for session in open_sessions}
    reason = "full_scan_completed" if scan_mode == ScanMode.FULL else "newer_session_detected"
    source_tag = "seal_all_open" if scan_mode == ScanMode.FULL else "seal_non_latest_open"

    for session_id in decision.sealed_session_ids:
        session = by_id.get(session_id)
        if session is None:
            continue
        transition_session(
            db,
            session.id,
            SessionAnalysisStatus.OPEN,
            SessionAnalysisStatus.SEALED,
            reason=reason,
            source=source_tag,
        )
        session.analysis_priority = priority
        sealed_envelope.append(
            SealedSessionInfo(
                session_id=session.id,
                source_id=source_id,
                priority=priority,
            )
        )
    if sealed_envelope:
        db.flush()
    return sealed_envelope


# --- runner.py ---
logger = logging.getLogger(__name__)


# --- runner.py ---
def acquire_source_advisory_lock(db: Session, source_id: int) -> None:
    """Acquire the per-source PG advisory transaction lock.

    The lock is released automatically when the caller's transaction
    ends (commit or rollback).
    """
    db.execute(select(func.pg_advisory_xact_lock(source_id)))


# --- runner.py ---
def _priority(scan_mode: str) -> str:
    return AnalysisPriority.HOT if scan_mode == ScanMode.HOT else AnalysisPriority.FULL


# --- runner.py ---
def _seal_buffer_eligible_open(
    db: Session,
    *,
    source_id: int,
    priority: str,
    now_utc: datetime,
) -> list[SealedSessionInfo]:
    """HOT-mode fallback: seal every ``OPEN`` session whose buffer has elapsed.

    Reproduces the legacy ``SessionBuilder._seal_by_buffer``
    branch. Sessions whose
    ``now - session_end_time > SEAL_BUFFER_SECONDS`` are
    flipped ``OPEN → SEALED`` via
    :func:`src.services.pipeline_state.transition_session`;
    the audit row is written in the same transaction as the
    CAS UPDATE.
    """
    normalized_now = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
    cutoff = normalized_now - timedelta(seconds=SEAL_BUFFER_SECONDS)
    sessions = (
        db.query(VideoSession)
        .filter(
            VideoSession.source_id == source_id,
            VideoSession.analysis_status == SessionAnalysisStatus.OPEN,
            VideoSession.session_end_time <= cutoff,
        )
        .all()
    )
    sealed: list[SealedSessionInfo] = []
    for session in sessions:
        transition_session(
            db,
            session.id,
            SessionAnalysisStatus.OPEN,
            SessionAnalysisStatus.SEALED,
            reason="seal_buffer_elapsed",
            source="seal_by_buffer",
        )
        session.analysis_priority = priority
        sealed.append(
            SealedSessionInfo(
                session_id=session.id,
                source_id=source_id,
                priority=priority,
            )
        )
    if sealed:
        db.flush()
    return sealed


# --- runner.py ---
def run(
    db: Session,
    *,
    source_id: int,
    root_path: str,
    scan_mode: str,
    scan_start: datetime,
    scan_end: datetime,
    cancel_check: Callable[[], None] | None = None,
    home_zone: ZoneInfo | None = None,
    now_utc: datetime | None = None,
) -> SessionBuildResult:
    """Run the whole pipeline; return :class:`SessionBuildResult`.

    Args:
        db: Caller-owned SQLAlchemy session.
        source_id: The :class:`VideoSource.id` under scan.
        root_path: Camera-recording directory (typically
            ``source.config_json["root_path"]``).
        scan_mode: :class:`ScanMode.HOT` or
            :class:`ScanMode.FULL`.
        scan_start: Inclusive lower bound for discovery.
        scan_end: Inclusive upper bound for discovery.
        cancel_check: Optional callable invoked from inside
            the parser's ``CANCEL_CHECK_INTERVAL`` throttle.
        home_zone: Camera-local zone (used by the parser to
            localize naive camera times to aware UTC). ``None``
            keeps the parser in legacy naive-output mode.
        now_utc: Override for the current time. ``None`` uses
            :func:`datetime.now(timezone.utc)`.

    Returns:
        A :class:`SessionBuildResult` with the same shape the
        legacy ``SessionBuilder.build`` produced.
    """
    result = SessionBuildResult()
    priority = _priority(scan_mode)
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)

    if cancel_check is not None:
        cancel_check()

    # Stage 1: acquire per-source advisory lock.
    acquire_source_advisory_lock(db, source_id)

    # Stage 2: discover files.
    parser = discovery.build_parser(root_path, timezone=home_zone)
    discovered = discovery.discover_files(
        parser=parser,
        scan_start=scan_start,
        scan_end=scan_end,
        cancel_check=cancel_check,
    )
    result.files_found = len(discovered)

    if not discovered:
        # HOT-mode fallback: seal every ``OPEN`` session
        # whose seal buffer has elapsed without any new file.
        if scan_mode == ScanMode.HOT:
            sealed = _seal_buffer_eligible_open(
                db,
                source_id=source_id,
                priority=priority,
                now_utc=now,
            )
            result.sessions_sealed = len(sealed)
            result.sealed_sessions = sealed
        return result

    # Stage 3: hash dedupe + insert.
    inserted: list[InsertedFile] = discovery.dedupe_files(
        db,
        source_id=source_id,
        discovered_files=discovered,
        cancel_check=cancel_check,
    )
    result.files_inserted = len(inserted)
    result.files_skipped = result.files_found - len(inserted)

    if cancel_check is not None:
        cancel_check()

    # Stage 4: pure reducer + persistence.
    open_sessions = persistence.find_open_sessions_for_source(db, source_id)
    existing_open = open_sessions[0] if open_sessions else None

    reducer_plan = reduce_files(inserted, existing_open_session=existing_open)

    sessions_created, sessions_updated = persistence.apply_file_actions(
        db,
        source_id=source_id,
        plan=reducer_plan,
        inserted_files=inserted,
    )
    result.sessions_created = sessions_created
    result.sessions_updated = sessions_updated

    if cancel_check is not None:
        cancel_check()

    # Stage 5: seal the residual ``OPEN`` sessions.
    open_sessions_post = persistence.find_open_sessions_for_source(db, source_id)
    decision = select_sessions_to_seal(
        scan_mode=scan_mode,
        open_sessions=open_sessions_post,
        now_utc=now,
    )
    sealed_envelope = apply_seal_transitions(
        db,
        source_id=source_id,
        scan_mode=scan_mode,
        decision=decision,
        open_sessions=open_sessions_post,
        priority=priority,
    )
    result.sessions_sealed = len(sealed_envelope)
    result.sealed_sessions = sealed_envelope

    return result


# --- runner.py ---
def run_hot(
    db: Session,
    *,
    source_id: int,
    root_path: str,
    scan_start: datetime,
    scan_end: datetime,
    cancel_check: Callable[[], None] | None = None,
    home_zone: ZoneInfo | None = None,
    now_utc: datetime | None = None,
) -> SessionBuildResult:
    """HOT-mode shortcut; preserves the legacy
    ``SessionBuilder.build(scan_mode=HOT, ...)`` interface."""
    return run(
        db,
        source_id=source_id,
        root_path=root_path,
        scan_mode=ScanMode.HOT,
        scan_start=scan_start,
        scan_end=scan_end,
        cancel_check=cancel_check,
        home_zone=home_zone,
        now_utc=now_utc,
    )


# --- runner.py ---
def run_full(
    db: Session,
    *,
    source_id: int,
    root_path: str,
    scan_start: datetime,
    scan_end: datetime,
    cancel_check: Callable[[], None] | None = None,
    home_zone: ZoneInfo | None = None,
    now_utc: datetime | None = None,
) -> SessionBuildResult:
    """FULL-mode shortcut; preserves the legacy
    ``SessionBuilder.build(scan_mode=FULL, ...)`` interface."""
    return run(
        db,
        source_id=source_id,
        root_path=root_path,
        scan_mode=ScanMode.FULL,
        scan_start=scan_start,
        scan_end=scan_end,
        cancel_check=cancel_check,
        home_zone=home_zone,
        now_utc=now_utc,
    )


__all__ = [
    "SealDecision",
    "acquire_source_advisory_lock",
    "apply_seal_transitions",
    "run",
    "run_full",
    "run_hot",
    "select_sessions_to_seal",
]
