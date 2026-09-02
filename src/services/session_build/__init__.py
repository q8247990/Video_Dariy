"""Session-build pipeline stage package (Wave 5, Todo 20).

The Celery task in :mod:`src.tasks.session_build` is the thin
wrapper that opens a ``task_db_session`` and binds the TaskLog;
this package owns the five execution stages the wrapper
delegates to:

* :mod:`.discovery` — walk the configured root path, parse
  the Xiaomi layout, return sorted ``DiscoveredFile`` rows.
  The parser integration is the only place this stage reaches
  for :mod:`src.adapters.xiaomi_parser`.
* :mod:`.dedupe` — the :class:`VideoFile` boundary. Queries
  existing file-path hashes in a single batch, inserts new
  rows under a savepoint so the unique-index race is silent,
  and returns the post-dedupe :class:`InsertedFile` list.
* :mod:`.reducer` — the **pure** reducer. Operates on in-memory
  records; tests can call :func:`reducer.reduce_files` against
  hand-built dataclasses without standing up SQLAlchemy.
* :mod:`.seal_policy` — half pure, half persistence. The pure
  half (:func:`select_sessions_to_seal`) decides which
  ``OPEN`` sessions to seal; the persistence half
  (:func:`apply_seal_transitions`) issues the
  ``OPEN → SEALED`` transitions through
  :func:`src.services.pipeline_state.transition_session`.
* :mod:`.persistence` — applies the reducer's
  :class:`ReducerPlan` to the database (new
  :class:`VideoSession` rows, ``VideoSessionFileRel``
  relations, session ``session_end_time`` widen).
* :mod:`.runner` — the **shared** orchestrator the Celery
  task delegates to. Hot and full share one body
  (:func:`runner.run`); :func:`runner.run_hot` /
  :func:`runner.run_full` are thin convenience wrappers that
  pin the ``scan_mode`` argument.

Stages are intentionally side-effect-free except for the
persistence module (``persistence``) and the persistence half
of ``seal_policy``. The reducer is the only module the test
suite can drive without a SQLAlchemy session.

The package deliberately avoids reaching into
:mod:`src.application.*` so the architecture-boundary test
keeps treating :mod:`src.services` as the pure-business-rules
layer (Todo 5). Analyzer dispatch is the only
:mod:`src.application.*` consumer and lives in the slim Celery
task — the runner returns the :class:`SealedSessionInfo`
envelope and the task iterates it.
"""

from __future__ import annotations

from src.services.session_build import dedupe, discovery, persistence, runner, seal_policy
from src.services.session_build.reducer import (
    EXTEND,
    NEW_SESSION,
    ReducerAppendAction,
    ReducerPlan,
    ReducerSessionToCreate,
    reduce_files,
)
from src.services.session_build.types import (
    DiscoveredFile,
    InsertedFile,
    SealedSessionInfo,
    SessionBuildResult,
)

__all__ = [
    "DiscoveredFile",
    "EXTEND",
    "InsertedFile",
    "NEW_SESSION",
    "ReducerAppendAction",
    "ReducerPlan",
    "ReducerSessionToCreate",
    "SealedSessionInfo",
    "SessionBuildResult",
    "dedupe",
    "discovery",
    "persistence",
    "reduce_files",
    "runner",
    "seal_policy",
]
