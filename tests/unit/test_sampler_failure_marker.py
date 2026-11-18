"""Sampler-failure classification, no-auto-retry guard, /metrics totals (todo 33).

Four groups of tests:

* **Guard (DB-free)** — the source tree proves that no module handling
  the sampler failure classes re-queues / retries them, and that the
  analysis-recovery policy (running/pending requeue + lease timeout only)
  is sampler-agnostic.
* **/metrics aggregate (DB-free)** — :func:`build_frame_sampling_payload`
  is a pure function of the stored counter document; the aggregate totals
  are the sums of the per-provider entries.
* **Failure classification (PostgreSQL)** — the orchestration failure
  path driven through the public ``analyze_session_task`` entry: a fake
  frame port raising :class:`SamplerUnavailable` records
  ``TaskLog.detail_json["failure_kind"] == "sampler_unavailable"`` on
  both the ``PARTIAL`` and the ``FAILED`` session transitions, with the
  per-provider failure counter incremented and ``calls_total`` untouched;
  an LLM-side failure records the non-sampler value (``"llm"``) and
  leaves the sampler failure counter at its baseline.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.application import container_holder as _container
from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import (
    FakeAnalysisPorts,
    ScriptedVisionGateway,
    ScriptedVisionGatewayFactory,
)
from src.db.metrics import FRAME_SAMPLING_COUNTERS_KEY, build_frame_sampling_payload
from src.infrastructure.frame_sampler import SamplerUnavailable
from src.models.app_runtime_state import AppRuntimeState
from src.models.event_record import EventRecord
from src.models.llm_provider import LLMProvider
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.analysis.execute import PROVISIONAL_FRAMES_FINGERPRINT
from src.services.analysis.plan import build_analysis_plan
from src.services.frame_sampling.types import FrameSample, FrameSet
from src.tasks import _container as _ports_holder
from src.tasks.analyzer import analyze_session_task

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

#: The two backend-visible sampler failure class names (structural match
#: in :mod:`src.services.frame_sampling.counters`).
SAMPLER_FAILURE_MARKERS = ("SamplerUnavailable", "SamplerDisabled")

#: Candidate files that reference the sampler failure classes and are
#: allowed to mention retry-ish words, each with its reason.
ALLOWED_RETRY_EXCEPTIONS: dict[str, str] = {
    # Owns the pre-existing internal HTTP-level retries (3 attempts,
    # deterministic backoff) of ONE logical acquisition — transport
    # retries inside the sampler call, not a task-level requeue.
    "infrastructure/frame_sampler/client.py": "internal HTTP retries (transport-level)",
    # Docstring mentions the heartbeat retry sweep (delivery outbox
    # recovery); no sampler-related retry logic.
    "db/metrics.py": "docstring: heartbeat retry sweep (unrelated)",
}


def _relative_py_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


# ---------------------------------------------------------------------------
# Guard: no auto-retry of sampler failures (DB-free)
# ---------------------------------------------------------------------------


def test_no_module_with_sampler_failure_classes_retries() -> None:
    """Every ``src/`` module referencing a sampler failure class must be
    free of retry / requeue logic, except the two documented owners.

    The existing ``claim.py`` deadlock retry is *not* a candidate: it
    references no sampler failure class (asserted below), so the guard
    does not need to exempt it.
    """
    candidates: list[Path] = []
    for path in _relative_py_files(SRC_ROOT):
        content = path.read_text(encoding="utf-8")
        if any(marker in content for marker in SAMPLER_FAILURE_MARKERS):
            candidates.append(path)

    # The scan must find the known owners, so a rename cannot silently
    # vacate the guard.
    relative = {path.relative_to(SRC_ROOT).as_posix() for path in candidates}
    assert "infrastructure/frame_sampler/client.py" in relative
    assert "services/frame_sampling/counters.py" in relative

    for path in candidates:
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in ALLOWED_RETRY_EXCEPTIONS:
            continue
        content = path.read_text(encoding="utf-8").lower()
        for token in ("retry", "requeue", "self.retry"):
            assert token not in content, (
                f"{rel} references a sampler failure class and contains "
                f"{token!r} — sampler failures must not be auto-retried "
                f"or re-queued (document the reason in ALLOWED_RETRY_EXCEPTIONS "
                f"if this is intentional)"
            )

    # claim.py's deadlock retry is unrelated to the sampler: it must stay
    # free of sampler-failure references so the guard above keeps applying
    # only to genuine sampler-failure handlers.
    claim_src = (SRC_ROOT / "services" / "analysis" / "claim.py").read_text(encoding="utf-8")
    for marker in SAMPLER_FAILURE_MARKERS:
        assert marker not in claim_src


def test_analysis_recovery_has_no_sampler_handling() -> None:
    """``analysis_recovery`` keeps only running/pending requeue + lease
    timeout: it must contain no sampler-failure handling at all."""
    recovery_src = (SRC_ROOT / "services" / "maintenance" / "analysis_recovery.py").read_text(
        encoding="utf-8"
    )
    for marker in (
        *SAMPLER_FAILURE_MARKERS,
        "is_frame_acquisition_failure",
        "failure_kind",
        "frame_sampling",
    ):
        assert marker not in recovery_src
    # The existing recovery logic is intact (running/pending requeue +
    # lease-timeout budget).
    assert "dispatch_analyze_session" in recovery_src
    assert "ANALYSIS_RECOVERY_MAX_ATTEMPTS" in recovery_src


# ---------------------------------------------------------------------------
# /metrics aggregate payload (DB-free)
# ---------------------------------------------------------------------------


def test_build_frame_sampling_payload_aggregates_per_provider() -> None:
    value = {
        "by_provider": {
            "1": {
                "calls_total": 3,
                "failures_total": 1,
                "frames_total": 12,
                "cache_hits_total": 2,
            },
            "2": {
                "calls_total": 1,
                "failures_total": 2,
                "frames_total": 4,
                "cache_hits_total": 0,
            },
        }
    }
    payload = build_frame_sampling_payload(value)
    assert payload["by_provider"] == [
        {
            "provider_id": 1,
            "calls_total": 3,
            "failures_total": 1,
            "frames_total": 12,
            "cache_hits_total": 2,
        },
        {
            "provider_id": 2,
            "calls_total": 1,
            "failures_total": 2,
            "frames_total": 4,
            "cache_hits_total": 0,
        },
    ]
    # Aggregate totals are the sums of the per-provider entries.
    for field in ("calls_total", "failures_total", "frames_total", "cache_hits_total"):
        assert payload[field] == sum(entry[field] for entry in payload["by_provider"])
    assert payload["calls_total"] == 4
    assert payload["failures_total"] == 3
    assert payload["frames_total"] == 16
    assert payload["cache_hits_total"] == 2


def test_build_frame_sampling_payload_tolerates_missing_and_malformed() -> None:
    empty = {
        "calls_total": 0,
        "failures_total": 0,
        "frames_total": 0,
        "cache_hits_total": 0,
        "by_provider": [],
    }
    assert build_frame_sampling_payload(None) == empty
    assert build_frame_sampling_payload({}) == empty
    assert build_frame_sampling_payload({"by_provider": "junk"}) == empty
    # A non-dict provider entry is skipped, not fabricated.
    assert build_frame_sampling_payload({"by_provider": {"1": "junk"}}) == empty


# ---------------------------------------------------------------------------
# Failure classification through the orchestration (PostgreSQL)
# ---------------------------------------------------------------------------

IMAGE_MODE_MAX_MODEL_LEN = 16384
IMAGE_MODE_MAX_OUTPUT_TOKENS = 2048
IMAGE_MODE_MAX_EDGE = 640

_BASELINE_COUNTERS = {
    "calls_total": 2,
    "failures_total": 1,
    "frames_total": 10,
    "cache_hits_total": 1,
}


@pytest.fixture
def analyzer_pg_env(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    """Truncate the analyzer tables and wire the task-layer container /
    ports holder; route task DB sessions through the migrated engine (same
    seam as the stages integration test). Requesting the engine fixture
    auto-marks the test as postgres.
    """
    with postgres_migrated_engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE session_analysis_checkpoint RESTART IDENTITY CASCADE")
        )
        conn.execute(sql_text("TRUNCATE TABLE event_record RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE pipeline_transition_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE app_runtime_state RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_session RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_file RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_session_file_rel RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_source RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE llm_provider RESTART IDENTITY CASCADE"))

    factory = ScriptedVisionGatewayFactory()
    factory.install_gateway(ScriptedVisionGateway(responses=[]))
    container = bootstrap_for_tests(llm_factory=factory)
    _container.set_container_for_tests(container)
    _ports_holder.set_analysis_ports_for_tests(FakeAnalysisPorts())

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = Session(bind=postgres_migrated_engine)
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr("src.tasks._analyzer_orchestration.task_db_session", _task_session)
    try:
        yield postgres_migrated_engine
    finally:
        _container.reset_container_for_tests()
        _ports_holder.reset_analysis_ports_for_tests()


def _seed_image_mode_provider(db: Session) -> LLMProvider:
    provider = LLMProvider(
        provider_name="sampler-failure-marker-pg",
        provider_type="vision_provider",
        api_base_url="http://example.invalid/v1",
        api_key="",
        model_name="vision-test",
        timeout_seconds=30,
        enabled=True,
        supports_vision=True,
        is_default_vision=True,
        extra_config_json={
            "frame_sampling": {
                "mode": "event",
                "min_frames": 2,
                "max_frames": 12,
                "probe_fps": 2.0,
                "max_edge": IMAGE_MODE_MAX_EDGE,
                "target_context_ratio": 0.9,
            },
            "max_model_len": IMAGE_MODE_MAX_MODEL_LEN,
            "max_output_tokens": IMAGE_MODE_MAX_OUTPUT_TOKENS,
        },
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def _seed_source(db: Session) -> VideoSource:
    source = VideoSource(
        source_name="sampler-failure-marker",
        camera_name="cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.commit()
    return source


def _seed_session_with_file(db: Session, source: VideoSource, video_file_path: str) -> VideoSession:
    video_session = VideoSession(
        source_id=source.id,
        session_start_time=datetime.now(timezone.utc),
        session_end_time=datetime.now(timezone.utc),
        total_duration_seconds=60,
        analysis_status="sealed",
    )
    db.add(video_session)
    db.flush()
    video_file = VideoFile(
        source_id=source.id,
        file_name=Path(video_file_path).name,
        file_path=video_file_path,
        storage_type="local_file",
        file_format="mp4",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
        duration_seconds=60,
        parse_status="parsed",
    )
    db.add(video_file)
    db.flush()
    db.add(
        VideoSessionFileRel(
            session_id=video_session.id,
            video_file_id=video_file.id,
            sort_index=0,
        )
    )
    db.commit()
    return video_session


def _seed_counter_baseline(db: Session, provider_id: int) -> None:
    db.add(
        AppRuntimeState(
            state_key=FRAME_SAMPLING_COUNTERS_KEY,
            state_value={"by_provider": {str(provider_id): dict(_BASELINE_COUNTERS)}},
        )
    )
    db.commit()


def _install_image_mode_ports(
    *,
    frame_set=None,
    frame_set_error: BaseException | None = None,
) -> FakeAnalysisPorts:
    """One-file one-sub-chunk image-mode ports (real plan, fake frame port)."""
    ports = FakeAnalysisPorts(
        chunks=1,
        sub_chunks_per_chunk=1,
        frame_set=frame_set,
        frame_set_error=frame_set_error,
    )
    ports.plan_analysis = build_analysis_plan
    _ports_holder.set_analysis_ports_for_tests(ports)
    return ports


def _set_responses(*responses: str) -> None:
    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    if factory._gateway is not None:
        factory._gateway.queue_responses(list(responses))
    else:
        factory.install_gateway(ScriptedVisionGateway(responses=list(responses)))


def _cache_hit_frame_set() -> FrameSet:
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 8 + b"\xff\xd9"
    return FrameSet(
        frames=tuple(FrameSample(t_seconds=float(i), jpeg_bytes=jpeg) for i in range(3)),
        source="cache",
        elapsed_ms=1,
    )


def _latest_task_log(db: Session, session_id: int) -> TaskLog:
    task_log = (
        db.query(TaskLog)
        .filter(TaskLog.task_target_id == session_id, TaskLog.task_type == "session_analysis")
        .order_by(TaskLog.id.desc())
        .first()
    )
    assert task_log is not None
    return task_log


@pytest.mark.postgres
def test_sampler_failure_records_failure_kind_and_counter(
    analyzer_pg_env: Engine, tmp_path: Path
) -> None:
    """Fake frame port raising SamplerUnavailable → TaskLog failure_kind
    ``sampler_unavailable``, session PARTIAL, zero events, failure counter
    +1 with calls_total unchanged."""
    engine = analyzer_pg_env
    video_file_path = tmp_path / "sampler-fail.mp4"
    video_file_path.write_bytes(b"\x00\x01\x02\x03")

    with Session(engine) as db:
        provider = _seed_image_mode_provider(db)
        provider_id = provider.id
        source = _seed_source(db)
        session = _seed_session_with_file(db, source, str(video_file_path))
        _seed_counter_baseline(db, provider_id)
        session_id = session.id

    _install_image_mode_ports(frame_set_error=SamplerUnavailable("sampler service down"))
    _set_responses(_recognition_result_json(0))
    with pytest.raises(SamplerUnavailable):
        analyze_session_task.run(session_id=session_id)

    with Session(engine) as db:
        task_log = _latest_task_log(db, session_id)
        assert task_log.status == "failed"
        detail: dict[str, Any] = task_log.detail_json or {}
        assert detail["failure_kind"] == "sampler_unavailable"
        # The pre-sampler frame-plan write survived the failure merge.
        assert "frame_plan" in detail
        video_session = db.query(VideoSession).filter_by(id=session_id).one()
        assert video_session.analysis_status == "partial"
        assert db.query(EventRecord).filter_by(session_id=session_id).count() == 0
        row = db.query(AppRuntimeState).filter_by(state_key=FRAME_SAMPLING_COUNTERS_KEY).one()
        entry = row.state_value["by_provider"][str(provider_id)]
        assert entry == {
            "calls_total": _BASELINE_COUNTERS["calls_total"],
            "failures_total": _BASELINE_COUNTERS["failures_total"] + 1,
            "frames_total": _BASELINE_COUNTERS["frames_total"],
            "cache_hits_total": _BASELINE_COUNTERS["cache_hits_total"],
        }


@pytest.mark.postgres
def test_sampler_failure_records_failure_kind_on_failed_transition(
    analyzer_pg_env: Engine, tmp_path: Path
) -> None:
    """The same marker must land on the FAILED transition: with the
    checkpoint already ``success`` (a resumed run), the pre-sampler claim
    leaves no ``processing`` row, so a sampler failure finalises the
    session as FAILED — and still carries ``failure_kind``."""
    engine = analyzer_pg_env
    video_file_path = tmp_path / "sampler-fail-2.mp4"
    video_file_path.write_bytes(b"\x00\x01\x02\x03")

    with Session(engine) as db:
        provider = _seed_image_mode_provider(db)
        source = _seed_source(db)
        session = _seed_session_with_file(db, source, str(video_file_path))
        plan = build_analysis_plan(db, session.id)
        db.add(
            SessionAnalysisCheckpoint(
                session_id=session.id,
                analysis_run_id=plan.analysis_run_id,
                chunk_index=0,
                sub_chunk_index=0,
                start_offset_seconds=0,
                input_fingerprint=PROVISIONAL_FRAMES_FINGERPRINT,
                state="success",
            )
        )
        _seed_counter_baseline(db, provider.id)
        session_id = session.id
        db.commit()

    _install_image_mode_ports(frame_set_error=SamplerUnavailable("sampler service down"))
    with pytest.raises(SamplerUnavailable):
        analyze_session_task.run(session_id=session_id)

    with Session(engine) as db:
        task_log = _latest_task_log(db, session_id)
        assert task_log.status == "failed"
        detail: dict[str, Any] = task_log.detail_json or {}
        assert detail["failure_kind"] == "sampler_unavailable"
        video_session = db.query(VideoSession).filter_by(id=session_id).one()
        assert video_session.analysis_status == "failed"
        assert db.query(EventRecord).filter_by(session_id=session_id).count() == 0


@pytest.mark.postgres
def test_llm_failure_records_llm_kind_and_leaves_sampler_counters(
    analyzer_pg_env: Engine, tmp_path: Path
) -> None:
    """Scripted gateway raising (LLM-side failure) → failure_kind ``llm``
    (the non-sampler value) and the sampler failure counter does not move."""
    engine = analyzer_pg_env
    video_file_path = tmp_path / "llm-fail.mp4"
    video_file_path.write_bytes(b"\x00\x01\x02\x03")

    with Session(engine) as db:
        provider = _seed_image_mode_provider(db)
        provider_id = provider.id
        source = _seed_source(db)
        session = _seed_session_with_file(db, source, str(video_file_path))
        _seed_counter_baseline(db, provider_id)
        session_id = session.id

    # Frames are acquired fine; the vision call itself raises.
    _install_image_mode_ports(frame_set=lambda p, path, plan: _cache_hit_frame_set())
    _set_responses("raise")
    with pytest.raises(RuntimeError):
        analyze_session_task.run(session_id=session_id)

    with Session(engine) as db:
        task_log = _latest_task_log(db, session_id)
        assert task_log.status == "failed"
        detail: dict[str, Any] = task_log.detail_json or {}
        assert detail["failure_kind"] == "llm"
        video_session = db.query(VideoSession).filter_by(id=session_id).one()
        assert video_session.analysis_status == "partial"
        assert db.query(EventRecord).filter_by(session_id=session_id).count() == 0
        row = db.query(AppRuntimeState).filter_by(state_key=FRAME_SAMPLING_COUNTERS_KEY).one()
        entry = row.state_value["by_provider"][str(provider_id)]
        # No sampler failure happened and the LLM call never succeeded, so
        # every counter stays at the pre-seeded baseline.
        assert entry == dict(_BASELINE_COUNTERS)


def _recognition_result_json(index: int) -> str:
    from src.services.video_analysis.schemas import (
        RecognitionResultDTO,
        RecognizedEventDTO,
        SessionSummaryDTO,
    )

    return RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text=f"summary-{index}",
            activity_level="medium",
            main_subjects=["PG"],
        ),
        events=[
            RecognizedEventDTO(
                offset_start_sec=1.0,
                offset_end_sec=5.0,
                event_type="member_appear",
                title=f"event-{index}",
                summary=f"event-{index}",
                detail=f"event-{index}",
                related_entities=[],
                observed_actions=[],
                interpreted_state=[],
                confidence=0.9,
            )
        ],
        analysis_notes=[],
    ).model_dump_json()
