"""Analysis-pipeline constants.

Single source of truth for tunables used by the analyzer stages. Anything
that needs to be re-imported across ``src/services/analysis/*.py`` modules
should live here (or in the test fixtures / config layer); keeping it in
one module avoids the parameter-passing-by-name that the original
monolithic task accumulated.
"""

from __future__ import annotations

# Number of frames the vLLM vision backend samples from a sub-chunk video
# payload. The product decision keeps raw_mp4 at 120 frames; do not
# re-introduce a keyframe-only path here — see
# ``docs/adr/0009-remove-keyframe-pipeline.md``.
RAW_MP4_NUM_FRAMES: int = 120

# Celery ``autoretry_for`` retry budget for the analyzer task. Used to
# distinguish transient PG deadlocks (``40P01`` / ``40001``) from
# permanent failures; after ``DEADLOCK_MAX_RETRIES`` the worker lets
# the task transition to FAILED and hands the lease back to
# task_maintenance.
DEADLOCK_MAX_RETRIES: int = 3

# Retry backoff baseline (seconds). The Celery ``self.retry`` countdown
# is ``2 ** self.request.retries`` so the first retry waits 1s.
DEADLOCK_RETRY_BACKOFF_SECONDS: int = 1

# Backoff schedule used while claiming a session that briefly disappears
# (e.g. another worker is mid-rollback). The total wait is < 4 seconds.
NOT_FOUND_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)

# PostgreSQL SQLSTATEs that the analyzer treats as transient and
# triggers a Celery retry for. ``40P01`` is ``deadlock_detected``,
# ``40001`` is ``serialization_failure``.
POSTGRES_RETRYABLE_SQLSTATES: frozenset[str] = frozenset({"40P01", "40001"})

__all__ = [
    "DEADLOCK_MAX_RETRIES",
    "DEADLOCK_RETRY_BACKOFF_SECONDS",
    "NOT_FOUND_RETRY_DELAYS_SECONDS",
    "POSTGRES_RETRYABLE_SQLSTATES",
    "RAW_MP4_NUM_FRAMES",
]
