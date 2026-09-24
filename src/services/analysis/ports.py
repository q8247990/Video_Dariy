"""Analyzer pipeline port surface.

The analyzer orchestration depends on three helpers that live outside
``src.services.analysis`` (media planning / payload building live in
``src.services.session_analysis_video`` + ``chunk_plan``; event replace
lives in the aggregator). They are bundled behind a narrow, frozen
:class:`AnalysisPorts` dataclass so:

* the orchestration can run unchanged in production (default wiring
  delegates back to the existing service helpers — zero behaviour
  change);
* tests inject a fake :class:`AnalysisPorts` through the task-layer
  holder (or the ``ports`` kwarg on
  :func:`src.tasks._analyzer_orchestration.run_session_analysis`) so the
  pipeline runs against a scripted media layout without monkey-patching
  private symbols;
* the production wiring remains in one place (here).

The dataclass fields are typed callables rather than Protocol classes:
the helpers are stateless functions with stable signatures. Lazy imports
inside :func:`build_analysis_ports` keep the module load-time graph
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.models.event_record import EventRecord
    from src.services.analysis.chunk_plan import AnalysisPlan


PlanAnalysisFn = Callable[["Session", int], "AnalysisPlan"]
VideoDataUrlFn = Callable[[str], str]
ReplaceSessionEventsFn = Callable[["Session", int, "list[EventRecord]"], int]


@dataclass(frozen=True)
class AnalysisPorts:
    """Frozen bundle of the helpers the analyzer orchestration depends on.

    The dataclass is frozen so a fake instance is also immutable (the
    production wiring must not drift under the feet of a running worker).
    """

    plan_analysis: PlanAnalysisFn
    video_data_url: VideoDataUrlFn
    replace_session_events: ReplaceSessionEventsFn


def build_analysis_ports() -> AnalysisPorts:
    """Return the production :class:`AnalysisPorts` bundle.

    Imports are lazy so the analyzer task's module load-time graph does
    not change.
    """
    from src.services.analysis.aggregator import _replace_session_events
    from src.services.analysis.chunk_plan import build_analysis_plan
    from src.services.session_analysis_video import build_video_data_url

    return AnalysisPorts(
        plan_analysis=build_analysis_plan,
        video_data_url=build_video_data_url,
        replace_session_events=_replace_session_events,
    )


__all__ = [
    "AnalysisPorts",
    "build_analysis_ports",
]
