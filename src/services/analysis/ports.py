"""Analyzer pipeline port surface (first wave, Todo "测试边界收敛").

The analyzer task previously reached into ``src.tasks._analyzer_orchestration``
for five internal helper symbols (``build_session_video_chunks`` /
``build_chunk_sub_chunks`` / ``session_chunk_from_sub_chunk`` /
``build_chunk_video_data_url`` / ``_replace_session_events``); the unit
and PG-integration tests in turn monkey-patched those same names in the
orchestration module's namespace, locking the orchestrator to a single
internal decomposition. This module extracts those five helpers behind a
narrow, frozen :class:`AnalysisPorts` dataclass so:

* the orchestration can run unchanged in production (default wiring
  delegates back to the existing service helpers — zero behaviour
  change);
* tests inject a fake :class:`AnalysisPorts` through the task-layer
  holder (or the ``ports`` kwarg on
  :func:`src.tasks._analyzer_orchestration.run_session_analysis`) so the
  pipeline runs against a scripted media layout without monkey-patching
  private symbols;
* the production wiring remains in one place (here) rather than
  scattered across every call site.

The dataclass fields are typed callables rather than Protocol classes:
the helpers are stateless functions with stable signatures, and the
existing services / aggregator module already owns the types those
callables accept and return. Adding a Protocol layer would be
boilerplate without runtime value.

Lazy imports inside :func:`build_analysis_ports` keep the module
load-time graph unchanged: ``src.services.session_analysis_video`` and
``src.services.analysis.aggregator`` continue to be imported only when
the production wiring is materialised (i.e. at the first call to
:func:`run_session_analysis`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.models.event_record import EventRecord
    from src.services.session_analysis_video import (
        SessionVideoChunk,
        SubChunk,
    )


PlanChunksFn = Callable[["Session", int, int], "list[SessionVideoChunk]"]
PlanSubChunksFn = Callable[["SessionVideoChunk", "Session", int], "list[SubChunk]"]
SubChunkAsChunkFn = Callable[["SubChunk", int], "SessionVideoChunk"]
ChunkVideoDataUrlFn = Callable[["SessionVideoChunk"], str]
ReplaceSessionEventsFn = Callable[["Session", int, "list[EventRecord]"], int]


@dataclass(frozen=True)
class AnalysisPorts:
    """Frozen bundle of the five helpers the analyzer orchestration depends on.

    The defaults wired by :func:`build_analysis_ports` call the existing
    service functions — replacing any one field with a fake is the only
    injection point the task-layer test fixture uses. The dataclass is
    frozen so a fake instance is also immutable (the production wiring
    must not drift under the feet of a running worker).
    """

    plan_chunks: PlanChunksFn
    plan_sub_chunks: PlanSubChunksFn
    sub_chunk_as_chunk: SubChunkAsChunkFn
    chunk_video_data_url: ChunkVideoDataUrlFn
    replace_session_events: ReplaceSessionEventsFn


def build_analysis_ports() -> AnalysisPorts:
    """Return the production :class:`AnalysisPorts` bundle.

    Imports are lazy so that the module load-time graph of the analyzer
    task does not change — ``src.services.session_analysis_video`` is
    already imported by :mod:`src.tasks._analyzer_orchestration` and
    :mod:`src.services.analysis.aggregator` is small enough to import
    here without disturbing the layer-boundary gate.
    """
    from src.services.analysis.aggregator import _replace_session_events
    from src.services.session_analysis_video import (
        build_chunk_sub_chunks,
        build_chunk_video_data_url,
        build_session_video_chunks,
        session_chunk_from_sub_chunk,
    )

    return AnalysisPorts(
        plan_chunks=build_session_video_chunks,
        plan_sub_chunks=build_chunk_sub_chunks,
        sub_chunk_as_chunk=session_chunk_from_sub_chunk,
        chunk_video_data_url=build_chunk_video_data_url,
        replace_session_events=_replace_session_events,
    )


__all__ = [
    "AnalysisPorts",
    "build_analysis_ports",
]
