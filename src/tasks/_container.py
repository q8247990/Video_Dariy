"""Analysis-port bundle holder for the Celery analyzer.

The analyzer pipeline reads its :class:`AnalysisPorts` bundle through
:func:`get_analysis_ports`; tests inject a scripted bundle through
:func:`set_analysis_ports_for_tests`. The holder stays ``None`` until
first access so importing this module does not pull in the composition
root (the production bundle is built through
:func:`src.application.bootstrap.build_analysis_ports_bundle` on first
use).
"""

from __future__ import annotations

from src.services.analysis.contracts import AnalysisPorts

_analysis_ports: AnalysisPorts | None = None


def get_analysis_ports() -> AnalysisPorts:
    global _analysis_ports
    if _analysis_ports is None:
        from src.application.bootstrap import build_analysis_ports_bundle

        _analysis_ports = build_analysis_ports_bundle()
    return _analysis_ports


def set_analysis_ports_for_tests(ports: AnalysisPorts) -> None:
    global _analysis_ports
    _analysis_ports = ports


def reset_analysis_ports_for_tests() -> None:
    global _analysis_ports
    _analysis_ports = None


__all__ = [
    "get_analysis_ports",
    "reset_analysis_ports_for_tests",
    "set_analysis_ports_for_tests",
]
