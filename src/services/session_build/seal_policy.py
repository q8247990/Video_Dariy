"""Seal-policy stage: decide which sessions should be SEALED, and apply the transitions.

The stage has two halves:

* :func:`select_sessions_to_seal` — the pure half. Given a
  snapshot of the source's ``OPEN`` sessions and the run's
  ``scan_mode``, return the :class:`VideoSession.id` values
  the runner must transition to ``SEALED``. The function has
  no SQLAlchemy / DB dependencies and is unit-testable with
  in-memory :class:`VideoSession` records.
* :func:`apply_seal_transitions` — the persistence half. The
  runner calls this after the reducer has been applied; it
  walks the snapshot and writes the ``OPEN → SEALED``
  transition via :func:`src.services.pipeline_state.transition_session`.
  The side effect is the only place the pipeline ever writes a
  ``PipelineTransitionLog`` row on this code path.

The seal matrix
===============

================  =======================================================
``scan_mode``     Sessions sealed by this build
================  =======================================================
``ScanMode.FULL``  Every ``OPEN`` session for the source (the scan
                range is complete; nothing more is expected).
``ScanMode.HOT`` (1) every ``OPEN`` session except the latest one
                (a new ``OPEN`` session confirms completeness
                for the older ones), and (2) the latest
                ``OPEN`` session once
                ``now - session_end_time > SEAL_BUFFER_SECONDS``.
================  =======================================================

The runner invokes :func:`apply_seal_transitions` after the
reducer has been applied, so the list of newly-sealed
sessions is the same shape (:class:`SealedSessionInfo`) the
analyzer dispatcher consumes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.models.video_session import VideoSession
from src.services.pipeline_constants import AnalysisPriority, ScanMode, SessionAnalysisStatus
from src.services.pipeline_state import transition_session
from src.services.session_build.constants import SEAL_BUFFER_SECONDS
from src.services.session_build.types import SealedSessionInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SealDecision:
    """Pure seal-policy output.

    :attr:`sealed_session_ids` is the list of
    :class:`VideoSession.id` values the runner must transition
    ``OPEN → SEALED``. The order matches the input list order so
    the persistence layer iterates deterministically.
    """

    sealed_session_ids: list[int] = field(default_factory=list)


def _priority_for_scan_mode(scan_mode: str) -> str:
    return AnalysisPriority.HOT if scan_mode == ScanMode.HOT else AnalysisPriority.FULL


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


def _select_open_sessions_in_order(
    sessions: list[VideoSession], *, scan_mode: str
) -> list[VideoSession]:
    if scan_mode == ScanMode.FULL:
        return sorted(sessions, key=lambda s: _as_aware_utc(s.session_start_time))
    return sorted(sessions, key=lambda s: _as_aware_utc(s.session_end_time))


def _buffer_eligible_ids(sessions: list[VideoSession], *, now_utc: datetime) -> list[int]:
    """Return the ids of sessions whose seal buffer has elapsed."""
    cutoff = _as_aware_utc(now_utc) - timedelta(seconds=SEAL_BUFFER_SECONDS)
    return [session.id for session in sessions if _as_aware_utc(session.session_end_time) <= cutoff]


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
        priority: :class:`AnalysisPriority` value the runner
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


__all__ = [
    "SealDecision",
    "apply_seal_transitions",
    "select_sessions_to_seal",
]
