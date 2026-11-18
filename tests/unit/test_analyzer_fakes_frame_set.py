"""Unit tests for the ``FakeAnalysisPorts`` frame-set port (todo 13).

Covers:

- the default fake returns a deterministic ``FrameSet`` of three uniform
  frames (``t_seconds`` 0.0 / 1.0 / 2.0) whose ``jpeg_bytes`` carry the
  JPEG SOI/EOI markers, plus one ``EventCandidate(0.0, 2.0, ("scene",))``,
  ``source="sampler"`` and ``elapsed_ms=12``;
- every invocation records exactly one ``(provider, video_path, plan)``
  entry on ``frame_set_calls`` with the exact positional arguments;
- a configured ``frame_set_error`` (e.g. ``SamplerUnavailable``) is
  raised unchanged (same instance) and the call is still recorded;
- a scripted ``frame_set`` override is forwarded verbatim;
- ``video_data_url`` keeps its video-mode behaviour (stable data URL,
  input path ignored);
- constructing an :class:`AnalysisPorts` bundle from the fake's own
  methods yields a frozen bundle whose ``frame_set`` is callable;
- importing ``src.application.bootstrap_fakes`` in a fresh interpreter
  does not load any ``src.infrastructure`` module (the fake must stay
  hermetic; the check result is printed from the subprocess).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from src.application.bootstrap_fakes import FakeAnalysisPorts
from src.infrastructure.frame_sampler.client import SamplerUnavailable
from src.services.analysis import AnalysisPorts
from src.services.frame_sampling.budget import FramePlan
from src.services.frame_sampling.types import EventCandidate, FrameSet

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _frame_plan() -> FramePlan:
    duration = 60.0
    frames = 3
    return FramePlan(
        frames=frames,
        timestamps=tuple(i * duration / frames for i in range(frames)),
        tokens_per_image=240,
        budget_tokens=frames * 240,
        clamp_reason="budget",
        duration_source="probe",
    )


def test_default_frame_set_returns_three_uniform_frames() -> None:
    """The unconfigured fake returns the documented deterministic FrameSet."""

    fake = FakeAnalysisPorts()
    plan = _frame_plan()

    result = fake.frame_set(object(), "/v/x.mp4", plan)

    assert isinstance(result, FrameSet)
    assert [frame.t_seconds for frame in result.frames] == [0.0, 1.0, 2.0]
    assert all(frame.reason == "uniform" for frame in result.frames)
    for frame in result.frames:
        # Tiny valid-looking JPEG: SOI marker, payload, EOI marker.
        assert frame.jpeg_bytes.startswith(b"\xff\xd8")
        assert frame.jpeg_bytes.endswith(b"\xff\xd9")
        assert len(frame.jpeg_bytes) > 4
    assert result.candidates == (EventCandidate(0.0, 2.0, ("scene",)),)
    assert result.source == "sampler"
    assert result.elapsed_ms == 12


def test_default_frame_set_is_fresh_per_invocation() -> None:
    """Each call yields an independent FrameSet (no shared mutable state)."""

    fake = FakeAnalysisPorts()
    plan = _frame_plan()

    first = fake.frame_set(object(), "/v/a.mp4", plan)
    second = fake.frame_set(object(), "/v/a.mp4", plan)

    assert first is not second
    assert first == second


def test_frame_set_records_one_call_per_invocation() -> None:
    """``frame_set_calls`` holds exactly the positional args of each call."""

    fake = FakeAnalysisPorts()
    provider = object()
    plan = _frame_plan()

    fake.frame_set(provider, "/v/a.mp4", plan)
    fake.frame_set(provider, "/v/b.mp4", plan)

    assert fake.frame_set_calls == [
        (provider, "/v/a.mp4", plan),
        (provider, "/v/b.mp4", plan),
    ]


def test_configured_frame_set_error_is_raised_unchanged() -> None:
    """A scripted exception propagates verbatim and the call is recorded."""

    error = SamplerUnavailable("sampler down")
    fake = FakeAnalysisPorts(frame_set_error=error)
    provider = object()
    plan = _frame_plan()

    with pytest.raises(SamplerUnavailable) as exc_info:
        fake.frame_set(provider, "/v/x.mp4", plan)

    assert exc_info.value is error
    assert fake.frame_set_calls == [(provider, "/v/x.mp4", plan)]


def test_scripted_frame_set_override_is_forwarded_verbatim() -> None:
    """A caller-supplied callable is used instead of the default."""

    scripted = FrameSet(frames=(), source="cache", elapsed_ms=1)
    seen: list[tuple[Any, str, Any]] = []

    def custom_frame_set(provider: Any, video_path: str, plan: Any) -> FrameSet:
        seen.append((provider, video_path, plan))
        return scripted

    fake = FakeAnalysisPorts(frame_set=custom_frame_set)
    provider = object()
    plan = _frame_plan()

    result = fake.frame_set(provider, "/v/x.mp4", plan)

    assert result is scripted
    assert seen == [(provider, "/v/x.mp4", plan)]
    assert fake.frame_set_calls == [(provider, "/v/x.mp4", plan)]


def test_video_data_url_keeps_video_mode_behaviour() -> None:
    """The video-mode port is untouched: stable data URL, path ignored."""

    fake = FakeAnalysisPorts(data_url="data:video/mp4;base64,ZZZZ")

    assert fake.video_data_url("/v/whatever.mp4") == "data:video/mp4;base64,ZZZZ"
    assert fake.video_data_url("/v/other.mp4") == "data:video/mp4;base64,ZZZZ"


def test_analysis_ports_bundle_from_fake_fields_is_frozen_and_callable() -> None:
    """A bundle built from the fake's methods is frozen and callable."""

    fake = FakeAnalysisPorts()
    bundle = AnalysisPorts(
        plan_analysis=fake.plan_analysis,
        video_data_url=fake.video_data_url,
        replace_session_events=fake.replace_session_events,
        frame_set=fake.frame_set,
    )

    assert callable(bundle.frame_set)
    # Bound methods compare by (__self__, __func__); identity is not stable
    # across attribute accesses, so use equality here.
    assert bundle.frame_set == fake.frame_set
    result = bundle.frame_set(object(), "/v/x.mp4", _frame_plan())
    assert [frame.t_seconds for frame in result.frames] == [0.0, 1.0, 2.0]

    with pytest.raises(FrozenInstanceError):
        bundle.frame_set = lambda *args: None  # type: ignore[method-assign]


def test_bootstrap_fakes_import_does_not_load_infrastructure() -> None:
    """Module load of the fakes must not pull in src.infrastructure.

    The check runs in a fresh interpreter and prints its result; the
    test session's own imports cannot mask it.
    """

    code = (
        "import sys\n"
        "import src.application.bootstrap_fakes\n"
        "loaded = [m for m in sys.modules if m.startswith('src.infrastructure')]\n"
        "print(','.join(sorted(loaded)) or 'NOT_LOADED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"fresh import of bootstrap_fakes failed:\n{result.stderr}"
    assert result.stdout.strip() == "NOT_LOADED", (
        f"infrastructure modules loaded at import: {result.stdout.strip()!r}"
    )
