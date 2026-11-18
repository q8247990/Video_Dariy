"""PostgreSQL integration tests for the /metrics frame-sampling counters (todo 12).

Drives the analyzer pipeline in **image mode** through the public
:func:`analyze_session_task` Celery entry against real PostgreSQL — one
successful run (cache-served frame set) and one sampler-failure run — then
asserts the per-provider counters surfaced by
:func:`src.db.metrics.metrics_snapshot` (the exact function the
``/metrics`` endpoint returns).

The counters are persisted under the ``AppRuntimeState`` key
``frame_sampling_counters`` (same app-runtime snapshot mechanism as the
heartbeat counters — no migration). The test pre-seeds that row with
non-zero baseline counts so the assertions cover deltas on top of prior
state.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

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
from src.db.metrics import FRAME_SAMPLING_COUNTERS_KEY, metrics_snapshot
from src.infrastructure.frame_sampler import SamplerUnavailable
from src.models.app_runtime_state import AppRuntimeState
from src.models.event_record import EventRecord
from src.models.llm_provider import LLMProvider
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.analysis.plan import build_analysis_plan
from src.services.frame_sampling.types import FrameSample, FrameSet
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks import _analyzer_orchestration  # noqa: F401
from src.tasks import _container as _ports_holder
from src.tasks.analyzer import analyze_session_task

pytestmark = pytest.mark.postgres

IMAGE_MODE_MAX_MODEL_LEN = 16384
IMAGE_MODE_MAX_OUTPUT_TOKENS = 2048
IMAGE_MODE_MAX_EDGE = 640


# ---------------------------------------------------------------------------
# Test isolation / base action (mirrors test_analyzer_stages_postgres.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _truncate_analysis_tables(postgres_migrated_engine: Engine) -> None:
    """Truncate every table this test touches, in FK-respecting order."""

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


@pytest.fixture(autouse=True)
def _base_action(
    postgres_migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Wire the task-layer container + ports holder; route task DB sessions
    through the migrated engine (same seam as the stages integration test).
    """

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
        yield
    finally:
        _container.reset_container_for_tests()
        _ports_holder.reset_analysis_ports_for_tests()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_image_mode_provider(db: Session) -> LLMProvider:
    provider = LLMProvider(
        provider_name="metrics-frame-sampling-pg",
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
        source_name="metrics-fs-pg",
        camera_name="cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.commit()
    return source


def _seed_session_with_file(
    db: Session,
    source: VideoSource,
    video_file_path: str,
) -> VideoSession:
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


def _recognition_result_json(index: int) -> str:
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


def _set_responses(*responses: str) -> None:
    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    if factory._gateway is not None:
        factory._gateway.queue_responses(list(responses))
    else:
        factory.install_gateway(ScriptedVisionGateway(responses=list(responses)))


def _cache_hit_frame_set() -> FrameSet:
    """A deterministic 3-frame cache-served set (``source="cache"``)."""
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 8 + b"\xff\xd9"
    return FrameSet(
        frames=tuple(FrameSample(t_seconds=float(i), jpeg_bytes=jpeg) for i in range(3)),
        source="cache",
        elapsed_ms=1,
    )


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_frame_sampling_counters_survive_success_and_failure_on_postgres(
    postgres_migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    """One success + one sampler failure through the orchestrator: the
    ``/metrics`` snapshot reports the per-provider deltas (calls /
    failures / frames / cache hits) on top of the pre-seeded baseline, and
    every pre-existing metric key keeps its shape.
    """
    ok_file = tmp_path / "ok.mp4"
    ok_file.write_bytes(b"\x00\x01\x02\x03")
    fail_file = tmp_path / "fail.mp4"
    fail_file.write_bytes(b"\x00\x01\x02\x03")

    with Session(postgres_migrated_engine) as db:
        provider = _seed_image_mode_provider(db)
        provider_id = provider.id
        source = _seed_source(db)
        ok_session = _seed_session_with_file(db, source, str(ok_file))
        fail_session = _seed_session_with_file(db, source, str(fail_file))
        # Pre-seed the app-runtime counter snapshot with a non-zero
        # baseline so the assertions prove incremental updates.
        db.add(
            AppRuntimeState(
                state_key=FRAME_SAMPLING_COUNTERS_KEY,
                state_value={
                    "by_provider": {
                        str(provider_id): {
                            "calls_total": 2,
                            "failures_total": 1,
                            "frames_total": 10,
                            "cache_hits_total": 1,
                        }
                    }
                },
            )
        )
        db.commit()
        # The commit above expires the session attributes; capture the ids
        # (lazy-reloaded while the session is still open) for the runs below.
        ok_session_id = ok_session.id
        fail_session_id = fail_session.id

    # --- Act 1: image-mode success run (cache-served frame set). ---
    ports = _install_image_mode_ports(frame_set=lambda p, path, plan: _cache_hit_frame_set())
    _set_responses(_recognition_result_json(0))
    result = analyze_session_task.run(session_id=ok_session_id)
    assert result["events_created"] == 1
    assert len(ports.frame_set_calls) == 1

    # --- Act 2: image-mode sampler failure run (new session). ---
    failure_ports = _install_image_mode_ports(
        frame_set_error=SamplerUnavailable("sampler service down")
    )
    _set_responses(_recognition_result_json(1))
    with pytest.raises(SamplerUnavailable):
        analyze_session_task.run(session_id=fail_session_id)
    assert len(failure_ports.frame_set_calls) == 1

    # --- Assert: counters via the endpoint function. ---
    body = metrics_snapshot(postgres_migrated_engine)
    frame_sampling = body["frame_sampling"]
    entries = {entry["provider_id"]: entry for entry in frame_sampling["by_provider"]}
    entry = entries[provider_id]
    # Baseline 2 calls + 1 success (cache-served, 3 frames); the failed
    # attempt bumps failures_total only (calls_total stays 3).
    assert entry["calls_total"] == 3
    assert entry["failures_total"] == 2
    assert entry["frames_total"] == 13
    assert entry["cache_hits_total"] == 2

    # Shape stability: the pre-existing /metrics keys are all still
    # present with their documented inner keys.
    assert set(body) == {
        "delivery",
        "task_recovery",
        "analysis_checkpoint",
        "frame_sampling",
    }
    assert set(body["delivery"]) == {"oldest_pending_seconds", "pending_count", "failed_count"}
    assert set(body["task_recovery"]) == {"leases_recovered_total", "last_heartbeat"}
    assert set(body["task_recovery"]["last_heartbeat"]) == {
        "leases_recovered",
        "unleased_recovered",
        "dispatched_hot",
        "logs_deleted",
        "missing_marked",
    }
    assert isinstance(body["analysis_checkpoint"], list)
    assert set(frame_sampling) == {
        "calls_total",
        "failures_total",
        "frames_total",
        "cache_hits_total",
        "by_provider",
    }
    for item in frame_sampling["by_provider"]:
        assert set(item) == {
            "provider_id",
            "calls_total",
            "failures_total",
            "frames_total",
            "cache_hits_total",
        }
    # Aggregate totals are the sums of the per-provider entries.
    for field in ("calls_total", "failures_total", "frames_total", "cache_hits_total"):
        assert frame_sampling[field] == sum(item[field] for item in frame_sampling["by_provider"])

    # The failure left the session PARTIAL with zero events (the failure
    # semantics are unchanged by the counter).
    with Session(postgres_migrated_engine) as verify:
        failed_session = verify.query(VideoSession).filter_by(id=fail_session_id).one()
        assert failed_session.analysis_status == "partial"
        assert verify.query(EventRecord).filter_by(session_id=fail_session_id).count() == 0


@pytest.mark.postgres
def test_frame_sampling_metrics_empty_before_any_image_analysis(
    postgres_migrated_engine: Engine,
) -> None:
    """No image-mode analysis yet: the counter key is absent from
    AppRuntimeState and the /metrics section carries zero totals with an
    empty ``by_provider`` list (no fabricated provider row).
    """
    body = metrics_snapshot(postgres_migrated_engine)
    assert body["frame_sampling"] == {
        "calls_total": 0,
        "failures_total": 0,
        "frames_total": 0,
        "cache_hits_total": 0,
        "by_provider": [],
    }
    assert set(body) == {
        "delivery",
        "task_recovery",
        "analysis_checkpoint",
        "frame_sampling",
    }
