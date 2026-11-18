"""Unit tests for the AnalysisPorts bundle and the frame-set port (todo 7).

Covers:

- ``build_analysis_ports`` takes the ``frame_set`` binding as an explicit
  required keyword (services must not import infrastructure; the adapter
  binding is supplied by the composition root) — a missing keyword fails
  fast with ``TypeError``;
- a bundle built from fakes forwards ``frame_set`` calls with exactly
  the three positional arguments and returns the fake's FrameSet;
- omitting the required ``frame_set`` field at dataclass construction
  fails fast (``TypeError``);
- the bundle stays frozen (``FrozenInstanceError`` on assignment);
- the composition root binds the infrastructure acquire entry point
  (``build_analysis_ports_bundle().frame_set is acquire_frame_set``);
- importing ``src.services.analysis`` in a fresh interpreter does not
  load ``src.infrastructure.frame_sampler.client`` at module load time
  (the check result is printed from the subprocess).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from src.application.bootstrap import build_analysis_ports_bundle
from src.infrastructure.frame_sampler.acquire import acquire_frame_set
from src.services.analysis import AnalysisPorts, build_analysis_ports
from src.services.frame_sampling.budget import FramePlan
from src.services.frame_sampling.types import FrameSet

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _frame_plan() -> FramePlan:
    duration = 60.0
    frames = 4
    return FramePlan(
        frames=frames,
        timestamps=tuple(i * duration / frames for i in range(frames)),
        tokens_per_image=240,
        budget_tokens=frames * 240,
        clamp_reason="budget",
        duration_source="probe",
    )


def _frame_set() -> FrameSet:
    return FrameSet(frames=(), source="sampler")


def _bundle(frame_set_fn: Any) -> AnalysisPorts:
    """Build an :class:`AnalysisPorts` from inert fakes plus ``frame_set_fn``."""

    return AnalysisPorts(
        plan_analysis=lambda db, session_id: None,
        video_data_url=lambda path: "data:video/mp4;base64,AAAA",
        replace_session_events=lambda db, session_id, events: 0,
        frame_set=frame_set_fn,
    )


def test_build_analysis_ports_requires_frame_set() -> None:
    """The frame-set binding is an explicit required keyword argument."""

    with pytest.raises(TypeError):
        build_analysis_ports()


def test_build_analysis_ports_binds_supplied_frame_set() -> None:
    """The supplied callable is bound verbatim on the returned bundle."""

    def fake_frame_set(provider: Any, video_path: str, plan: FramePlan) -> FrameSet:
        return _frame_set()

    ports = build_analysis_ports(frame_set=fake_frame_set)

    assert callable(ports.frame_set)
    assert ports.frame_set is fake_frame_set


def test_bundle_forwards_frame_set_calls_positionally() -> None:
    """An injected fake sees exactly the three positional arguments."""

    calls: list[tuple[Any, ...]] = []
    scripted = _frame_set()

    def fake_frame_set(provider: Any, video_path: str, plan: FramePlan) -> FrameSet:
        calls.append((provider, video_path, plan))
        return scripted

    ports = _bundle(fake_frame_set)
    provider = object()
    plan = _frame_plan()

    result = ports.frame_set(provider, "/v/x.mp4", plan)

    assert result is scripted
    assert calls == [(provider, "/v/x.mp4", plan)]


def test_missing_frame_set_field_raises_type_error() -> None:
    """The required field fails fast at construction."""

    with pytest.raises(TypeError):
        AnalysisPorts(
            plan_analysis=lambda db, session_id: None,
            video_data_url=lambda path: "data:video/mp4;base64,AAAA",
            replace_session_events=lambda db, session_id, events: 0,
        )


def test_bundle_is_frozen() -> None:
    """Assignment to a bound port raises FrozenInstanceError."""

    ports = _bundle(lambda provider, video_path, plan: _frame_set())

    with pytest.raises(FrozenInstanceError):
        ports.frame_set = acquire_frame_set


def test_composition_root_binds_infrastructure_acquire() -> None:
    """The composition root supplies the infrastructure acquire entry point."""

    ports = build_analysis_ports_bundle()

    assert callable(ports.frame_set)
    assert ports.frame_set is acquire_frame_set


def test_analysis_import_does_not_load_sampler_client() -> None:
    """Module-load of src.services.analysis must not pull in the sampler client.

    The check runs in a fresh interpreter and prints its result; the test
    session's own imports cannot mask it.
    """

    code = (
        "import sys\n"
        "import src.services.analysis\n"
        "loaded = 'src.infrastructure.frame_sampler.client' in sys.modules\n"
        "print('LOADED' if loaded else 'NOT_LOADED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"fresh import of src.services.analysis failed:\n{result.stderr}"
    assert result.stdout.strip() == "NOT_LOADED", (
        f"sampler client imported at module load: {result.stdout.strip()!r}"
    )
