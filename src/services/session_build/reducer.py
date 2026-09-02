"""Pure session reducer: merge consecutive files into VideoSession segments.

This module is the **pure** stage of the pipeline — it has no
SQLAlchemy session, no :class:`VideoSession` / :class:`VideoFile`
references and no I/O. The function :func:`reduce_files` consumes
the dedupe output and a snapshot of the source's currently-open
session, and returns a :class:`ReducerPlan` describing exactly
what the persistence stage must insert / append. Tests can call
it against in-memory records without standing up SQLite.

Plan layout
===========

The plan is two parallel lists:

* ``append_actions`` — every dedupe-inserted file, in the
  same order discovery produced. Each action carries a
  ``target_session`` that the persistence stage must point
  the new :class:`VideoSessionFileRel` row at.

  ``target_session`` is one of two literal values:

  - ``EXTEND`` (the string sentinel below) means "append to
    the still-OPEN ``VideoSession`` recorded in
    :attr:`ReducerPlan.extend_session_id`".
  - ``NEW_SESSION`` (the string sentinel below) means "append
    to the brand-new session at
    :attr:`ReducerAppendAction.new_session_index` inside
    :attr:`ReducerPlan.new_sessions``".

* ``new_sessions`` — the brand-new ``VideoSession`` rows the
  persistence layer must insert, in the chronological order
  the reducer opened them (a build can open many sessions
  because every over-gap boundary opens a new one).

The merge contract:

* Two consecutive files whose ``start_time - previous_session_end_time``
  is in ``[0, MERGE_GAP_SECONDS]`` belong to the same session.
  ``previous_session_end_time`` is either the pre-existing
  ``OPEN`` session's ``session_end_time`` or the most-recent
  new session the reducer emitted inside this same build.
* Files further apart open a new session.
* The very first file in the input seeds a new session when
  no ``existing_open_session`` is provided.
* Out-of-order input is the caller's responsibility. The
  discovery stage sorts by ``start_time`` ascending; tests
  must pre-sort their own input if they construct fixtures
  by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Sentinel: ``action.target_session == EXTEND`` means the
#: action extends the source's pre-existing ``OPEN`` session
#: (``ReducerPlan.extend_session_id``).
EXTEND: str = "extend"

#: Sentinel: ``action.target_session == NEW_SESSION`` means the
#: action targets the brand-new session recorded at
#: ``action.new_session_index`` in :attr:`ReducerPlan.new_sessions`.
NEW_SESSION: str = "new"


@dataclass(frozen=True)
class ReducerAppendAction:
    """One row in :class:`ReducerPlan.append_actions`.

    ``file_hash`` is the deduplication key the persistence
    layer uses to look up the actual :class:`VideoFile` row.

    ``target_session`` is the string sentinel that names
    which session the action appends to: either :data:`EXTEND`
    (against :attr:`ReducerPlan.extend_session_id`) or
    :data:`NEW_SESSION` (against
    ``plan.new_sessions[action.new_session_index]``).

    ``sort_index`` is the per-session
    :class:`VideoSessionFileRel.sort_index` the persistence
    layer must write.
    """

    file_hash: str
    target_session: str
    new_session_index: int = 0
    sort_index: int = 0


@dataclass(frozen=True)
class ReducerSessionToCreate:
    """A new :class:`VideoSession` the persistence layer must insert.

    The first :class:`VideoFile` in the new session is captured
    here as :attr:`first_file_hash` so the persistence layer
    can write the initial :class:`VideoSessionFileRel` and
    populate ``session_start_time`` / ``session_end_time`` /
    ``total_duration_seconds`` in one transaction.
    """

    first_file_hash: str
    first_video_file_id: int
    session_start_time: datetime
    session_end_time: datetime
    total_duration_seconds: int


@dataclass
class ReducerPlan:
    """Pure reducer output.

    * :attr:`extend_session_id` — the still-``OPEN``
      ``VideoSession.id`` the new files may append to. ``None``
      when no open session exists yet for the source.
    * :attr:`next_sort_index` — the ``sort_index`` the
      persistence layer must use for the **first** ``EXTEND``
      action the reducer emitted; subsequent appends derive
      their ``sort_index`` from
      :attr:`ReducerAppendAction.sort_index`.
    * :attr:`append_actions` — the files the reducer chose to
      either extend :attr:`extend_session_id` or to seed new
      sessions. The order matches the dedupe output.
    * :attr:`new_sessions` — the new ``VideoSession`` rows the
      persistence layer must create, in the chronological
      order the reducer opened them.
    """

    extend_session_id: Optional[int] = None
    next_sort_index: int = 0
    append_actions: list[ReducerAppendAction] = field(default_factory=list)
    new_sessions: list[ReducerSessionToCreate] = field(default_factory=list)


def _calculate_session_duration_seconds(start_time: datetime, end_time: datetime) -> int:
    return max(0, int((end_time - start_time).total_seconds()))


@dataclass(frozen=True)
class _State:
    """Pure reducer running state.

    Each iteration of :func:`reduce_files` consults /
    mutates a single :class:`_State` instance; the helper
    :func:`_decide_target` reads it as an
    ``existing_open_session`` proxy.
    """

    extend_id: Optional[int]
    extend_end_time: Optional[datetime]
    next_extend_sort: int
    next_new_sort: int
    new_session_index: Optional[int]
    new_session_end_time: Optional[datetime]


def _initial_state(existing_open_session: Optional[Any]) -> _State:
    if existing_open_session is None:
        return _State(None, None, 0, 0, None, None)
    return _State(
        extend_id=int(existing_open_session.id),
        extend_end_time=existing_open_session.session_end_time,
        next_extend_sort=0,
        next_new_sort=0,
        new_session_index=None,
        new_session_end_time=None,
    )


def _within_merge_gap(start_time: datetime, end_time: datetime, *, merge_gap_seconds: int) -> bool:
    gap = (start_time - end_time).total_seconds()
    return 0 <= gap <= merge_gap_seconds


def _decide_target(
    state: _State, video_file: Any, *, merge_gap_seconds: int
) -> tuple[Optional[str], int]:
    """Return ``(target, sort_index)`` for the next file; ``None`` triggers a new session.

    Pure decision: no mutation. The caller updates
    ``state`` after the helper decides.
    """
    if state.extend_end_time is not None and _within_merge_gap(
        video_file.start_time, state.extend_end_time, merge_gap_seconds=merge_gap_seconds
    ):
        return EXTEND, state.next_extend_sort
    if state.new_session_end_time is not None and _within_merge_gap(
        video_file.start_time,
        state.new_session_end_time,
        merge_gap_seconds=merge_gap_seconds,
    ):
        sort_index = 0 if state.next_new_sort == 0 else state.next_new_sort
        return NEW_SESSION, sort_index
    return None, 0


def _build_new_session(file_hash: str, video_file: Any) -> ReducerSessionToCreate:
    return ReducerSessionToCreate(
        first_file_hash=file_hash,
        first_video_file_id=int(video_file.id),
        session_start_time=video_file.start_time,
        session_end_time=video_file.end_time,
        total_duration_seconds=_calculate_session_duration_seconds(
            video_file.start_time,
            video_file.end_time,
        ),
    )


def _update_state_after_extend(state: _State, video_file: Any) -> _State:
    return _State(
        extend_id=state.extend_id,
        extend_end_time=video_file.end_time,
        next_extend_sort=state.next_extend_sort + 1,
        next_new_sort=0,
        new_session_index=None,
        new_session_end_time=None,
    )


def _update_state_after_new_session(state: _State, video_file: Any, new_index: int) -> _State:
    return _State(
        extend_id=state.extend_id,
        extend_end_time=state.extend_end_time,
        next_extend_sort=state.next_extend_sort,
        next_new_sort=1,
        new_session_index=new_index,
        new_session_end_time=video_file.end_time,
    )


def _update_state_after_new_session_append(state: _State, video_file: Any) -> _State:
    return _State(
        extend_id=state.extend_id,
        extend_end_time=state.extend_end_time,
        next_extend_sort=state.next_extend_sort,
        next_new_sort=state.next_new_sort + 1,
        new_session_index=state.new_session_index,
        new_session_end_time=video_file.end_time,
    )


def reduce_files(
    inserted_files: list[Any],
    *,
    existing_open_session: Optional[Any] = None,
    merge_gap_seconds: int = 1,
) -> ReducerPlan:
    """Compute the merge plan; no DB I/O, no mutation.

    Args:
        inserted_files: Post-dedupe list of objects with a
            ``file_path_hash`` attribute and a ``video_file``
            attribute carrying ``id`` / ``start_time`` /
            ``end_time``. :class:`VideoFile` /
            :class:`src.services.session_build.types.InsertedFile`
            satisfy both; tests pass custom dataclasses for
            the pure-stage scenarios.
        existing_open_session: The still-``OPEN``
            :class:`VideoSession` the new files may extend
            (``None`` when no open session exists for the
            source).
        merge_gap_seconds: Override for the merge window. The
            default (``1``) matches the
            :data:`src.services.session_build.constants.MERGE_GAP_SECONDS`
            constant; tests use larger windows to assert the
            over-gap branch.

    Returns:
        A :class:`ReducerPlan`. The persistence stage must
        create one :class:`VideoSession` per entry in
        :attr:`ReducerPlan.new_sessions` and one
        :class:`VideoSessionFileRel` per entry in
        :attr:`ReducerPlan.append_actions`.
    """
    state = _initial_state(existing_open_session)
    plan = ReducerPlan(extend_session_id=state.extend_id)

    if not inserted_files:
        return plan

    for inserted in inserted_files:
        file_hash = str(inserted.file_path_hash)
        video_file = inserted.video_file
        target, sort_index = _decide_target(state, video_file, merge_gap_seconds=merge_gap_seconds)

        if target is None:
            new_session = _build_new_session(file_hash, video_file)
            plan.new_sessions.append(new_session)
            new_index = len(plan.new_sessions) - 1
            state = _update_state_after_new_session(state, video_file, new_index)
            plan.append_actions.append(
                ReducerAppendAction(
                    file_hash=file_hash,
                    target_session=NEW_SESSION,
                    new_session_index=new_index,
                    sort_index=0,
                )
            )
        elif target == EXTEND:
            plan.append_actions.append(
                ReducerAppendAction(
                    file_hash=file_hash,
                    target_session=EXTEND,
                    sort_index=sort_index,
                )
            )
            state = _update_state_after_extend(state, video_file)
        else:
            assert state.new_session_index is not None
            plan.append_actions.append(
                ReducerAppendAction(
                    file_hash=file_hash,
                    target_session=NEW_SESSION,
                    new_session_index=state.new_session_index,
                    sort_index=sort_index,
                )
            )
            state = _update_state_after_new_session_append(state, video_file)

    plan.next_sort_index = state.next_extend_sort
    return plan


__all__ = [
    "EXTEND",
    "NEW_SESSION",
    "ReducerAppendAction",
    "ReducerPlan",
    "ReducerSessionToCreate",
    "reduce_files",
]
