"""Hot / full runner: orchestrate the five stages into one transaction.

The runner is the **shared** body the slim Celery task in
:mod:`src.tasks.session_build` delegates to; the only
difference between the hot and the full variant is the
scan-window policy and the seal matrix. The runner body
itself is scan-mode agnostic: it always executes the same
five stages in the same order.

Stages (in execution order)
===========================

1. **acquire** — open a PG advisory transaction lock on
   ``source_id`` so concurrent ``hot`` vs ``full`` workers
   serialize (PG only; SQLite is a no-op so unit tests pass).
2. **discover** — walk the configured root path, parse the
   Xiaomi directory layout, return sorted ``DiscoveredFile``
   rows. Empty discovery for ``ScanMode.HOT`` triggers the
   pure seal-buffer sweep; for ``ScanMode.FULL`` the runner
   returns immediately.
3. **dedupe** — query existing ``file_path_hash`` rows,
   insert the new ``VideoFile`` rows, return the post-dedupe
   ``InsertedFile`` list.
4. **reduce + apply** — pure reducer computes the merge plan
   against the source's latest ``OPEN`` session; the
   persistence stage creates the new ``VideoSession`` rows
   and writes the ``VideoSessionFileRel`` relations.
5. **seal** — pure seal policy returns the session ids to
   transition; the persistence half writes the
   ``OPEN → SEALED`` transition through
   :func:`src.services.pipeline_state.transition_session`.

The runner does **no** analyzer dispatch. Analyzer dispatch is
the slim Celery task's seam to the outbox dispatcher; the
runner returns the :class:`SessionBuildResult` and the slim
task iterates ``sealed_sessions``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.video_session import VideoSession
from src.services.pipeline_constants import AnalysisPriority, ScanMode, SessionAnalysisStatus
from src.services.pipeline_state import transition_session
from src.services.session_build import dedupe, discovery, persistence, seal_policy
from src.services.session_build.constants import SEAL_BUFFER_SECONDS
from src.services.session_build.reducer import reduce_files
from src.services.session_build.types import (
    InsertedFile,
    SealedSessionInfo,
    SessionBuildResult,
)

logger = logging.getLogger(__name__)


def acquire_source_advisory_lock(db: Session, source_id: int) -> None:
    """Acquire the per-source PG advisory transaction lock.

    No-op for dialects other than PG; unit tests on SQLite do
    not need any locking. The lock is released automatically
    when the caller's transaction ends (commit or rollback).
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(source_id)))


def _priority(scan_mode: str) -> str:
    return AnalysisPriority.HOT if scan_mode == ScanMode.HOT else AnalysisPriority.FULL


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


def run(
    db: Session,
    *,
    source_id: int,
    root_path: str,
    scan_mode: str,
    scan_start: datetime,
    scan_end: datetime,
    cancel_check: Optional[Callable[[], None]] = None,
    home_zone: Optional[ZoneInfo] = None,
    now_utc: Optional[datetime] = None,
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
    inserted: list[InsertedFile] = dedupe.dedupe_files(
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
    decision = seal_policy.select_sessions_to_seal(
        scan_mode=scan_mode,
        open_sessions=open_sessions_post,
        now_utc=now,
    )
    sealed_envelope = seal_policy.apply_seal_transitions(
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


def run_hot(
    db: Session,
    *,
    source_id: int,
    root_path: str,
    scan_start: datetime,
    scan_end: datetime,
    cancel_check: Optional[Callable[[], None]] = None,
    home_zone: Optional[ZoneInfo] = None,
    now_utc: Optional[datetime] = None,
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


def run_full(
    db: Session,
    *,
    source_id: int,
    root_path: str,
    scan_start: datetime,
    scan_end: datetime,
    cancel_check: Optional[Callable[[], None]] = None,
    home_zone: Optional[ZoneInfo] = None,
    now_utc: Optional[datetime] = None,
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
    "acquire_source_advisory_lock",
    "run",
    "run_full",
    "run_hot",
]
