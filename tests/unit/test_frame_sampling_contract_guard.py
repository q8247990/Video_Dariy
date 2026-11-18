"""Guard: the slimmed frame-sampling contract stays slim (todo 37).

Source-level assertions that two removed pieces cannot silently come back:

- the sampler client carries no concurrency bound — ``build_frame_set``
  builds a fresh :class:`FrameSamplerClient` per call, so a per-instance
  ``BoundedSemaphore`` could never bound anything across requests (real
  parallelism is bounded by the Celery worker count);
- :class:`FrameSet` has no ``candidates`` field (the sampler's candidate
  windows have no backend consumer; selection happens inside the sampler).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from src.services.frame_sampling.types import FrameSet

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLIENT_SOURCE_PATH = PROJECT_ROOT / "src" / "infrastructure" / "frame_sampler" / "client.py"


def test_slimmed_contract_has_no_concurrency_bound_or_candidates() -> None:
    client_source = CLIENT_SOURCE_PATH.read_text(encoding="utf-8")
    assert "BoundedSemaphore" not in client_source, (
        "the per-call client instance makes any semaphore bound inert; "
        "concurrency is bounded by the Celery worker count"
    )
    assert "queue_limit" not in client_source

    frame_set_fields = {field.name for field in dataclasses.fields(FrameSet)}
    assert "candidates" not in frame_set_fields
    assert frame_set_fields == {"frames", "source", "elapsed_ms"}
