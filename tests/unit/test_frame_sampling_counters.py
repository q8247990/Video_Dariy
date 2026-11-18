"""Unit tests for the frame-sampling acquisition counter helpers (todo 12).

Covers the pure classification (:func:`is_frame_acquisition_failure`) and
the transactional increment semantics on real PostgreSQL (the ``pg_db``
fixture): row auto-creation, per-provider isolation, baseline
accumulation, and the success / failure field rules.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.metrics import FRAME_SAMPLING_COUNTERS_KEY
from src.infrastructure.frame_sampler import SamplerDisabled, SamplerUnavailable
from src.models.app_runtime_state import AppRuntimeState
from src.services.frame_sampling import (
    is_frame_acquisition_failure,
    record_frame_sampling_failure,
    record_frame_sampling_success,
)


def _get_counters_row(db: Session) -> AppRuntimeState | None:
    return db.query(AppRuntimeState).filter_by(state_key=FRAME_SAMPLING_COUNTERS_KEY).one_or_none()


def _entry(row: AppRuntimeState, provider_id: int) -> dict:
    return row.state_value["by_provider"][str(provider_id)]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_is_frame_acquisition_failure_matches_sampler_client_classes() -> None:
    assert is_frame_acquisition_failure(SamplerUnavailable("down")) is True
    assert is_frame_acquisition_failure(SamplerDisabled("off")) is True
    assert is_frame_acquisition_failure(RuntimeError("nope")) is False
    assert is_frame_acquisition_failure(ValueError("nope")) is False


def test_is_frame_acquisition_failure_rejects_same_name_from_other_module() -> None:
    class _LookalikeSamplerUnavailable(RuntimeError):
        pass

    # A same-named exception defined elsewhere is not the sampler client's.
    _LookalikeSamplerUnavailable.__module__ = "somewhere.else"
    assert is_frame_acquisition_failure(_LookalikeSamplerUnavailable("nope")) is False


# ---------------------------------------------------------------------------
# Increment semantics (real PostgreSQL, pg_db outer-transaction rollback)
# ---------------------------------------------------------------------------


def test_success_creates_row_and_counts_calls_and_frames(pg_db: Session) -> None:
    record_frame_sampling_success(pg_db, provider_id=7, frame_count=12, frame_source="sampler")

    row = _get_counters_row(pg_db)
    assert row is not None
    entry = _entry(row, 7)
    assert entry == {
        "calls_total": 1,
        "failures_total": 0,
        "frames_total": 12,
        "cache_hits_total": 0,
    }


def test_success_with_cache_source_counts_cache_hit(pg_db: Session) -> None:
    record_frame_sampling_success(pg_db, provider_id=7, frame_count=3, frame_source="cache")

    entry = _entry(_get_counters_row(pg_db), 7)
    assert entry["calls_total"] == 1
    assert entry["frames_total"] == 3
    assert entry["cache_hits_total"] == 1


def test_failure_counts_failures_only(pg_db: Session) -> None:
    record_frame_sampling_success(pg_db, provider_id=7, frame_count=5, frame_source="sampler")
    record_frame_sampling_failure(pg_db, provider_id=7)

    entry = _entry(_get_counters_row(pg_db), 7)
    assert entry == {
        "calls_total": 1,
        "failures_total": 1,
        "frames_total": 5,
        "cache_hits_total": 0,
    }


def test_counters_accumulate_over_seeded_baseline(pg_db: Session) -> None:
    pg_db.add(
        AppRuntimeState(
            state_key=FRAME_SAMPLING_COUNTERS_KEY,
            state_value={
                "by_provider": {
                    "7": {
                        "calls_total": 2,
                        "failures_total": 1,
                        "frames_total": 10,
                        "cache_hits_total": 1,
                    }
                }
            },
        )
    )
    pg_db.flush()

    record_frame_sampling_success(pg_db, provider_id=7, frame_count=3, frame_source="cache")
    record_frame_sampling_failure(pg_db, provider_id=7)

    entry = _entry(_get_counters_row(pg_db), 7)
    assert entry == {
        "calls_total": 3,
        "failures_total": 2,
        "frames_total": 13,
        "cache_hits_total": 2,
    }


def test_counters_are_isolated_per_provider(pg_db: Session) -> None:
    record_frame_sampling_success(pg_db, provider_id=1, frame_count=4, frame_source="sampler")
    record_frame_sampling_failure(pg_db, provider_id=2)

    row = _get_counters_row(pg_db)
    assert _entry(row, 1) == {
        "calls_total": 1,
        "failures_total": 0,
        "frames_total": 4,
        "cache_hits_total": 0,
    }
    assert _entry(row, 2) == {
        "calls_total": 0,
        "failures_total": 1,
        "frames_total": 0,
        "cache_hits_total": 0,
    }


def test_increment_tolerates_malformed_stored_shape(pg_db: Session) -> None:
    """A hand-crafted / partially-filled stored document must not crash the
    increment: missing fields default to 0, unknown entries are kept.
    """
    pg_db.add(
        AppRuntimeState(
            state_key=FRAME_SAMPLING_COUNTERS_KEY,
            state_value={"by_provider": {"7": {"calls_total": 1}}},
        )
    )
    pg_db.flush()

    record_frame_sampling_success(pg_db, provider_id=7, frame_count=2, frame_source="sampler")

    entry = _entry(_get_counters_row(pg_db), 7)
    assert entry["calls_total"] == 2
    assert entry["frames_total"] == 2
    assert entry["failures_total"] == 0
    assert entry["cache_hits_total"] == 0
