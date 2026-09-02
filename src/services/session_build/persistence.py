"""Persistence stage: apply the reducer plan to the database.

The reducer is a **pure** function and so cannot issue any SQL.
The persistence stage owns every SQLAlchemy write that turns the
:class:`ReducerPlan` into :class:`VideoSession` /
:class:`VideoSessionFileRel` rows.

Plan encoding
=============

The reducer encodes two kinds of append actions in
:attr:`ReducerPlan.append_actions`:

* ``target_session = EXTEND`` — the action extends the
  pre-existing ``OPEN`` session whose id is
  :attr:`ReducerPlan.extend_session_id`. The persistence
  stage loads it once at the start of the function and reuses
  the row across all the plan's ``EXTEND`` actions.

* ``target_session = NEW_SESSION`` — the action seeds / extends
  the brand-new session at
  ``plan.new_sessions[action.new_session_index]``. The
  persistence stage walks ``plan.new_sessions`` in index
  order, materialising each into a fresh
  :class:`VideoSession` row before applying any actions.

The function returns the
``(sessions_created, sessions_updated)`` counters the slim
Celery task logs in its final ``TaskLog`` detail. The session
``session_end_time`` ``max(...)`` widen and
``total_duration_seconds`` recompute is applied here, not in
the reducer (the reducer is pure).

Idempotency: the helper commits via the caller's session. The
uniqueness of ``uk_session_file_rel(session_id, video_file_id)``
and the reducer's decision to never emit the same file twice
mean the function is safe to call once per scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.services.pipeline_constants import SessionAnalysisStatus
from src.services.session_build.reducer import (
    EXTEND,
    NEW_SESSION,
    ReducerAppendAction,
    ReducerPlan,
    ReducerSessionToCreate,
)
from src.services.session_build.types import InsertedFile

logger = logging.getLogger(__name__)


def _calculate_session_duration_seconds(start_time: datetime, end_time: datetime) -> int:
    return max(0, int((end_time - start_time).total_seconds()))


def find_open_sessions_for_source(
    db: Session,
    source_id: int,
    *,
    order_by_end_desc: bool = True,
) -> list[VideoSession]:
    """Return the source's still-``OPEN`` sessions.

    Defaults to ordering by ``session_end_time`` descending so
    the reducer's ``existing_open_session`` argument picks the
    most-recent session — the one the dedupe will extend if
    the new file falls inside the merge-gap window. The seal
    policy re-sorts internally so this order is just a
    snapshot convenience.
    """
    query = db.query(VideoSession).filter(
        VideoSession.source_id == source_id,
        VideoSession.analysis_status == SessionAnalysisStatus.OPEN,
    )
    if order_by_end_desc:
        query = query.order_by(VideoSession.session_end_time.desc())
    return query.all()


def _next_rel_sort_index(db: Session, session_id: int) -> int:
    latest_rel = (
        db.query(VideoSessionFileRel)
        .filter(VideoSessionFileRel.session_id == session_id)
        .order_by(VideoSessionFileRel.sort_index.desc())
        .first()
    )
    if latest_rel is None:
        return 0
    return latest_rel.sort_index + 1


def _create_session_with_first_file(
    db: Session,
    source_id: int,
    new_session: ReducerSessionToCreate,
    first_video_file_id: int,
) -> VideoSession:
    session = VideoSession(
        source_id=source_id,
        session_start_time=new_session.session_start_time,
        session_end_time=new_session.session_end_time,
        total_duration_seconds=new_session.total_duration_seconds,
        analysis_status=SessionAnalysisStatus.OPEN,
    )
    db.add(session)
    db.flush()

    rel = VideoSessionFileRel(
        session_id=session.id,
        video_file_id=first_video_file_id,
        sort_index=0,
    )
    db.add(rel)
    return session


def _append_file(
    db: Session,
    session: VideoSession,
    video_file_id: int,
    sort_index: int,
    video_file_end_time: datetime,
) -> None:
    rel = VideoSessionFileRel(
        session_id=session.id,
        video_file_id=video_file_id,
        sort_index=sort_index,
    )
    db.add(rel)
    session.session_end_time = max(session.session_end_time, video_file_end_time)
    session.total_duration_seconds = _calculate_session_duration_seconds(
        session.session_start_time,
        session.session_end_time,
    )


def _index_inserted_files(inserted_files: list[InsertedFile]) -> dict[str, VideoFile]:
    return {inserted.file_path_hash: inserted.video_file for inserted in inserted_files}


def _materialize_new_sessions(
    db: Session,
    source_id: int,
    plan: ReducerPlan,
    by_hash: dict[str, VideoFile],
) -> dict[int, VideoSession]:
    """Create one :class:`VideoSession` per entry in ``plan.new_sessions``.

    The initial ``(sort_index=0)`` :class:`VideoSessionFileRel`
    is written by :func:`_create_session_with_first_file`. Skips
    any new session whose first file disappeared from
    ``by_hash`` (shouldn't happen; defensive).
    """
    new_session_by_index: dict[int, VideoSession] = {}
    for index, new_session in enumerate(plan.new_sessions):
        first_video_file = by_hash.get(new_session.first_file_hash)
        if first_video_file is None:
            continue
        session = _create_session_with_first_file(
            db,
            source_id,
            new_session,
            first_video_file_id=first_video_file.id,
        )
        new_session_by_index[index] = session
    return new_session_by_index


def _load_extend_session(db: Session, plan: ReducerPlan) -> tuple[Optional[VideoSession], int]:
    """Return the pre-existing ``OPEN`` session and its next sort index.

    Returns ``(None, 0)`` when the plan has no ``extend_session_id``
    or the session was deleted between plan and apply.
    """
    if plan.extend_session_id is None:
        return None, 0
    extend_session = (
        db.query(VideoSession).filter(VideoSession.id == plan.extend_session_id).first()
    )
    if extend_session is None:
        return None, 0
    return extend_session, _next_rel_sort_index(db, extend_session.id)


@dataclass
class _PersistContext:
    """Running state used while iterating ``plan.append_actions``.

    Holding mutable counters in one place lets each per-action
    handler stay tiny — the handlers return a ``bool`` that the
    outer loop counts. ``next_in_batch_sort`` tracks the
    per-brand-new-session ``sort_index`` after the
    ``(sort_index=0)`` initial relation has been emitted.
    """

    db: Session
    by_hash: dict[str, VideoFile]
    new_session_by_index: dict[int, VideoSession]
    extend_session: Optional[VideoSession]
    extend_next_sort: int
    next_in_batch_sort: dict[int, int] = field(default_factory=dict)


def _handle_extend_action(
    ctx: _PersistContext,
    action: ReducerAppendAction,
    video_file: VideoFile,
) -> bool:
    if ctx.extend_session is None:
        return False
    _append_file(
        ctx.db,
        ctx.extend_session,
        video_file_id=video_file.id,
        sort_index=ctx.extend_next_sort,
        video_file_end_time=video_file.end_time,
    )
    ctx.extend_next_sort += 1
    return True


def _handle_new_session_action(
    ctx: _PersistContext,
    action: ReducerAppendAction,
    video_file: VideoFile,
) -> bool:
    target_session = ctx.new_session_by_index.get(action.new_session_index)
    if target_session is None:
        return False
    # ``sort_index=0`` is already taken by the
    # ``_create_session_with_first_file`` initial relation;
    # subsequent appends inside the same mid-batch session use
    # ``1, 2, 3, ...``.
    if action.sort_index == 0:
        return True
    sort_index = ctx.next_in_batch_sort.get(action.new_session_index, 1)
    ctx.next_in_batch_sort[action.new_session_index] = sort_index + 1
    _append_file(
        ctx.db,
        target_session,
        video_file_id=video_file.id,
        sort_index=sort_index,
        video_file_end_time=video_file.end_time,
    )
    return True


_ACTION_HANDLERS: dict[str, Callable[[_PersistContext, ReducerAppendAction, VideoFile], bool]] = {
    EXTEND: _handle_extend_action,
    NEW_SESSION: _handle_new_session_action,
}


def _apply_actions(ctx: _PersistContext, plan: ReducerPlan) -> int:
    """Iterate ``plan.append_actions`` and dispatch to the per-action handlers.

    Returns the count of actions that wrote (or counted toward)
    a :class:`VideoSessionFileRel` row. ``video_file is None``
    and unknown ``target_session`` are silently skipped to keep
    the same observable behaviour as the pre-refactor function.
    """
    sessions_updated = 0
    for action in plan.append_actions:
        video_file = ctx.by_hash.get(action.file_hash)
        if video_file is None:
            continue
        handler = _ACTION_HANDLERS.get(action.target_session)
        if handler is None:
            logger.warning("Unknown reducer target_session=%s", action.target_session)
            continue
        if handler(ctx, action, video_file):
            sessions_updated += 1
    return sessions_updated


def apply_file_actions(
    db: Session,
    *,
    source_id: int,
    plan: ReducerPlan,
    inserted_files: list[InsertedFile],
) -> tuple[int, int]:
    """Apply the :class:`ReducerPlan` to the database.

    The persistence stage is the only writer of
    :class:`VideoSession` and :class:`VideoSessionFileRel`
    rows during a build. The function:

    1. Creates one :class:`VideoSession` per entry in
       :attr:`ReducerPlan.new_sessions` and emits the initial
       ``(sort_index=0)`` :class:`VideoSessionFileRel` row in
       one ``flush()``.
    2. Loads the :attr:`ReducerPlan.extend_session_id` session
       (if any) and computes its next ``sort_index`` from the
       database (so a re-run picks up where the previous run
       left off).
    3. Iterates :attr:`ReducerPlan.append_actions`, writes the
       :class:`VideoSessionFileRel` row, and updates the
       target session's ``session_end_time`` /
       ``total_duration_seconds``.

    Args:
        db: Caller-owned SQLAlchemy session.
        source_id: The :class:`VideoSource.id` under scan.
        plan: The pure :class:`ReducerPlan` from
            :func:`src.services.session_build.reducer.reduce_files`.
        inserted_files: Post-dedupe :class:`InsertedFile` list,
            ordered identically to ``plan.append_actions``.

    Returns:
        ``(sessions_created, sessions_updated)``. ``sessions_created``
        is the count of brand-new sessions; ``sessions_updated``
        is the count of files appended to either an existing
        ``OPEN`` session or a brand-new session that the reducer
        created in this build.
    """
    if not plan.append_actions:
        return 0, 0

    by_hash = _index_inserted_files(inserted_files)
    new_session_by_index = _materialize_new_sessions(db, source_id, plan, by_hash)
    extend_session, extend_next_sort = _load_extend_session(db, plan)

    ctx = _PersistContext(
        db=db,
        by_hash=by_hash,
        new_session_by_index=new_session_by_index,
        extend_session=extend_session,
        extend_next_sort=extend_next_sort,
    )
    sessions_updated = _apply_actions(ctx, plan)

    return len(new_session_by_index), sessions_updated


__all__ = [
    "apply_file_actions",
    "find_open_sessions_for_source",
]
