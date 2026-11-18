"""Per-provider frame-sampling acquisition counters (todo 12).

The analyzer orchestration increments these counters in the image-input
path so the ``/metrics`` surface can report how the frame sampler is
doing per provider.

**Storage (no migration).** The counters live in the existing
:class:`~src.models.app_runtime_state.AppRuntimeState` JSON snapshot
column under the key :data:`~src.db.metrics.FRAME_SAMPLING_COUNTERS_KEY`
(the same app-runtime mechanism the heartbeat uses for
``heartbeat_last_counters``)::

    {"by_provider": {"<provider_id>": {"calls_total": int,
                                       "failures_total": int,
                                       "frames_total": int,
                                       "cache_hits_total": int}}}

**Transactional semantics.** Neither helper commits: the caller adds the
increment to whatever transaction it already owns, so the counter write
lands atomically with the business write it accompanies — the
sub-chunk checkpoint ``success`` row (write side) or the checkpoint
``error`` + TaskLog ``FAILED`` finalisation (failure side). A reader
therefore never observes a counter without its checkpoint row.

**Counting unit: one logical acquisition attempt.** The sampler HTTP
client retries internally (up to 3 attempts); the orchestration makes
exactly one ``frame_set`` port call per sub-chunk, so the counters
count logical acquisition attempts, not HTTP attempts. A sampler-side
``ExtractionFailed`` (HTTP 422) surfaces to the backend as
:class:`~src.infrastructure.frame_sampler.client.SamplerUnavailable`
and counts as exactly one failure.

**Concurrency.** The row is read under ``SELECT ... FOR UPDATE`` and
created with a savepoint-guarded insert, so two workers incrementing the
same provider in parallel serialise on the row lock (READ COMMITTED
re-reads the winner's value after the lock is released).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.metrics import FRAME_SAMPLING_COUNTER_FIELDS, FRAME_SAMPLING_COUNTERS_KEY
from src.models.app_runtime_state import AppRuntimeState

logger = logging.getLogger(__name__)

#: The frame-sampler client's failure types are identified structurally
#: (defining module + class name) because the task layer that classifies
#: exceptions is architecturally forbidden from importing
#: ``src.infrastructure`` (see tests/architecture).
_FRAME_SAMPLER_CLIENT_MODULE = "src.infrastructure.frame_sampler.client"
_FRAME_SAMPLER_FAILURE_NAMES = frozenset({"SamplerUnavailable", "SamplerDisabled"})


def is_frame_acquisition_failure(exc: BaseException) -> bool:
    """Return True when ``exc`` is a frame-sampler acquisition failure.

    ``ExtractionFailed`` (raised inside the sampler service) reaches the
    backend as an HTTP 422 rejection, which the client converts to
    ``SamplerUnavailable`` — so both backend-visible failure classes
    cover it.
    """
    return (
        type(exc).__module__ == _FRAME_SAMPLER_CLIENT_MODULE
        and type(exc).__name__ in _FRAME_SAMPLER_FAILURE_NAMES
    )


def record_frame_sampling_success(
    db: Session,
    *,
    provider_id: int,
    frame_count: int,
    frame_source: str | None = None,
) -> None:
    """Count one successful frame acquisition for ``provider_id``.

    ``calls_total`` +1 and ``frames_total`` +``frame_count``; a
    cache-served acquisition (``frame_source == "cache"``) additionally
    bumps ``cache_hits_total``. Commits nothing — the caller's
    transaction (the checkpoint success write) commits the increment.
    """
    _increment(
        db,
        provider_id=provider_id,
        calls=1,
        frames=frame_count,
        cache_hits=1 if frame_source == "cache" else 0,
    )


def record_frame_sampling_failure(db: Session, *, provider_id: int) -> None:
    """Count one failed frame acquisition for ``provider_id``.

    ``failures_total`` +1 only — ``calls_total`` stays untouched for the
    failed attempt. Commits nothing — the caller's transaction (the
    checkpoint failure write) commits the increment.
    """
    _increment(db, provider_id=provider_id, failures=1)


def _increment(
    db: Session,
    *,
    provider_id: int,
    calls: int = 0,
    failures: int = 0,
    frames: int = 0,
    cache_hits: int = 0,
) -> None:
    row = _acquire_counter_row(db)
    value = row.state_value if isinstance(row.state_value, dict) else {}
    raw_by_provider = value.get("by_provider")
    # Every level is copied into a FRESH dict (values are ints — a shallow
    # copy is complete). In-place mutation of the stored dict would alias
    # the session's committed snapshot, so a content-equal reassignment
    # would not be detected as a change by SQLAlchemy and no UPDATE would
    # be emitted.
    by_provider: dict[str, Any] = dict(raw_by_provider) if isinstance(raw_by_provider, dict) else {}
    key = str(provider_id)
    existing = by_provider.get(key)
    entry: dict[str, Any] = (
        dict(existing)
        if isinstance(existing, dict)
        else dict.fromkeys(FRAME_SAMPLING_COUNTER_FIELDS, 0)
    )
    entry["calls_total"] = int(entry.get("calls_total", 0)) + calls
    entry["failures_total"] = int(entry.get("failures_total", 0)) + failures
    entry["frames_total"] = int(entry.get("frames_total", 0)) + frames
    entry["cache_hits_total"] = int(entry.get("cache_hits_total", 0)) + cache_hits
    by_provider[key] = entry
    row.state_value = {"by_provider": by_provider}


def _acquire_counter_row(db: Session) -> AppRuntimeState:
    """Return the counter row, creating it if missing (savepoint-guarded).

    ``FOR UPDATE`` serialises concurrent increments on the same row; the
    create path uses a savepoint so a lost insert race (a concurrent
    transaction committed the row first) degrades to a re-read instead
    of failing the caller's transaction.
    """
    row = (
        db.query(AppRuntimeState)
        .filter_by(state_key=FRAME_SAMPLING_COUNTERS_KEY)
        .with_for_update()
        .one_or_none()
    )
    if row is not None:
        return row
    try:
        with db.begin_nested():
            db.add(
                AppRuntimeState(
                    state_key=FRAME_SAMPLING_COUNTERS_KEY, state_value={"by_provider": {}}
                )
            )
            db.flush()
    except IntegrityError:
        # The concurrent winner's row is committed now (READ COMMITTED
        # sees it); the savepoint rolled back only our INSERT.
        row = (
            db.query(AppRuntimeState)
            .filter_by(state_key=FRAME_SAMPLING_COUNTERS_KEY)
            .with_for_update()
            .one()
        )
    else:
        row = db.query(AppRuntimeState).filter_by(state_key=FRAME_SAMPLING_COUNTERS_KEY).one()
    return row


__all__ = [
    "is_frame_acquisition_failure",
    "record_frame_sampling_failure",
    "record_frame_sampling_success",
]
