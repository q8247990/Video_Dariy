"""Wave G (todo 34) — the session frame budget caps frames acquired per analysis run.

Drives :func:`src.tasks.analyzer.analyze_session_task` end-to-end against
real PostgreSQL (``pg_db_factory``, per the repo unit-test convention) with
a scripted :class:`~src.application.bootstrap_fakes.ScriptedVisionGateway`
and a fake ``frame_set`` port, mirroring
``test_analyzer_frame_plan.py``. The registry read is exercised directly:
each test seeds the ``frame_sampling_session_frame_limit`` SystemConfig
row through :func:`src.services.system_config_registry.set_config`, so the
orchestration's single per-run ``get_config`` read is covered in the same
unit cases (no separate ``-m postgres`` case needed — the unit suite runs
on real PG).

The frame plan is pinned to an exact frame count per file via
``min_frames == max_frames == K`` (the four-gate clamp then yields
``K`` for any budget that affords ``K``), so the budget arithmetic is
deterministic:

* limit=0 — off: all files analysed, no ``frame_budget_exhausted`` key,
  one LLM call per file;
* limit=25, 3 files x 10 frames — exactly 2 files analysed (10+10=20, the
  third would exceed), sampler called exactly twice, session PARTIAL,
  TaskLog detail carries
  ``frame_budget_exhausted == {limit: 25, frames_acquired: 20,
  files_analyzed: 2, files_skipped: 1}``;
* limit=10, a file planning 15 — immediate exhausted state: zero sampler
  calls, zero LLM calls, PARTIAL, detail explains;
* the ``frame_plan`` single-key merge is preserved: the final
  ``detail_json["frame_plan"]`` is the LAST analysed file's plan (the
  pre-sampler write is a single overwritten key — the existing single-key
  behaviour from todo 33; there is no per-file frame_plan history), and
  it survives next to ``frame_budget_exhausted`` without clobbering.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.application import container_holder as _container
from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import (
    FakeAnalysisPorts,
    ScriptedVisionGateway,
    ScriptedVisionGatewayFactory,
)
from src.core.pipeline_constants import TaskStatus
from src.models.event_record import EventRecord
from src.models.llm_provider import LLMProvider
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.analysis.plan import build_analysis_plan
from src.services.frame_sampling.types import FrameSample, FrameSet
from src.services.system_config_registry import FRAME_SAMPLING_SESSION_FRAME_LIMIT, set_config
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks import _container as _ports_holder
from src.tasks.analyzer import analyze_session_task

SessionFactory = Callable[[], Session]

MAX_MODEL_LEN = 32768
MAX_OUTPUT_TOKENS = 2048
MAX_EDGE = 640


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFramePort:
    """Deterministic ``frame_set`` port: one :class:`FrameSample` per plan timestamp."""

    def __init__(self, *, elapsed_ms: int = 42, source: str = "sampler") -> None:
        self.calls: list[tuple[Any, str, Any]] = []
        self.results: list[FrameSet] = []
        self.elapsed_ms = elapsed_ms
        self.source = source

    def __call__(self, provider: Any, video_path: str, plan: Any) -> FrameSet:
        self.calls.append((provider, video_path, plan))
        frames = tuple(
            FrameSample(
                t_seconds=timestamp,
                jpeg_bytes=f"jpeg-frame-{index:04d}-{int(timestamp * 10):04d}".encode(),
                reason="uniform",
            )
            for index, timestamp in enumerate(plan.timestamps)
        )
        frame_set = FrameSet(frames=frames, source=self.source, elapsed_ms=self.elapsed_ms)
        self.results.append(frame_set)
        return frame_set


# ---------------------------------------------------------------------------
# Base action — task-layer holder + DB-env seam
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _base_action(pg_db_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Wire the container / ports holder and route ``task_db_session`` to PG.

    Mirrors ``test_analyzer_frame_plan._base_action``; each test installs its
    own scripted ports bundle and seeds the frame-limit SystemConfig row.
    """

    factory = ScriptedVisionGatewayFactory()
    gateway = ScriptedVisionGateway(responses=[])
    factory.install_gateway(gateway)
    container = bootstrap_for_tests(llm_factory=factory)
    _container.set_container_for_tests(container)

    @contextmanager
    def _task_session() -> Iterator[Session]:
        db = pg_db_factory()
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
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_provider(db: Session, *, pinned_frames: int) -> LLMProvider:
    """Seed the image-mode vision provider with a plan pinned to ``pinned_frames``.

    ``min_frames == max_frames == K`` forces :func:`compute_frame_plan` to
    yield exactly ``K`` frames (any budget that affords ``K``, which the
    generous ``MAX_MODEL_LEN`` guarantees), so the budget arithmetic below
    is fully deterministic.
    """
    extra_config_json: dict[str, Any] = {
        "frame_sampling": {
            "mode": "uniform",
            "min_frames": pinned_frames,
            "max_frames": pinned_frames,
            "probe_fps": 2.0,
            "max_edge": MAX_EDGE,
            "target_context_ratio": 0.9,
        },
        "max_model_len": MAX_MODEL_LEN,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    provider = LLMProvider(
        provider_name="frame-budget-test",
        provider_type="vision_provider",
        api_base_url="http://example.invalid/v1",
        api_key="",
        model_name="vision-test",
        timeout_seconds=30,
        enabled=True,
        supports_vision=True,
        is_default_vision=True,
        extra_config_json=extra_config_json,
    )
    db.add(provider)
    db.flush()
    return provider


def _seed_source_session(
    db: Session,
    *,
    files: list[str],
) -> int:
    """Seed source + sealed session + one video file per path (60 s each)."""
    source = VideoSource(
        source_name="客厅",
        camera_name="cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.flush()

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 15, 10, 0, 0),
        session_end_time=datetime(2026, 3, 15, 10, 5, 0),
        total_duration_seconds=300,
        analysis_status="sealed",
    )
    db.add(session)
    db.flush()

    for sort_index, file_path in enumerate(files):
        video_file = VideoFile(
            source_id=source.id,
            file_name=Path(file_path).name,
            file_path=file_path,
            start_time=datetime(2026, 3, 15, 10, 0, 0),
            end_time=datetime(2026, 3, 15, 10, 1, 0),
            duration_seconds=60,
        )
        db.add(video_file)
        db.flush()
        db.add(
            VideoSessionFileRel(
                session_id=session.id,
                video_file_id=video_file.id,
                sort_index=sort_index,
            )
        )
        db.flush()
    return session.id


def _install_ports(frame_port: FakeFramePort) -> FakeAnalysisPorts:
    """Install the scripted ports bundle with the real plan (file granularity)."""
    ports = FakeAnalysisPorts(chunks=1, sub_chunks_per_chunk=1)
    ports.plan_analysis = build_analysis_plan
    ports.frame_set = frame_port
    _ports_holder.set_analysis_ports_for_tests(ports)
    return ports


def _recognition_result_json(index: int) -> str:
    return RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text=f"summary-{index}",
            activity_level="medium",
            main_subjects=["爸爸"],
        ),
        events=[
            RecognizedEventDTO(
                offset_start_sec=1,
                offset_end_sec=5,
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


def _latest_gateway() -> ScriptedVisionGateway:
    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    return factory.gateways[-1]


def _set_responses(*responses: str) -> None:
    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    if factory._gateway is not None:
        factory._gateway.queue_responses(list(responses))
    else:
        factory.install_gateway(ScriptedVisionGateway(responses=list(responses)))


def _write_video_file(path: Path, name: str, payload: bytes = b"\x00\x01fake-mp4-bytes") -> str:
    file_path = path / name
    file_path.write_bytes(payload)
    return str(file_path)


def _run_analysis(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
    *,
    frame_port: FakeFramePort,
    pinned_frames: int,
    limit: int,
    file_count: int,
) -> int:
    """Seed provider/session/SystemConfig row and run one analysis pass."""
    file_paths = [
        _write_video_file(tmp_path, f"cam{index}.mp4") for index in range(1, file_count + 1)
    ]
    db = pg_db_factory()
    try:
        _seed_provider(db, pinned_frames=pinned_frames)
        session_id = _seed_source_session(db, files=file_paths)
        set_config(db, FRAME_SAMPLING_SESSION_FRAME_LIMIT, limit)
        db.commit()
    finally:
        db.close()
    _install_ports(frame_port)
    _set_responses(*[_recognition_result_json(index) for index in range(file_count)])
    analyze_session_task.run(session_id=session_id)
    return session_id


def _latest_task_log(pg_db_factory: SessionFactory, session_id: int) -> TaskLog:
    verify_db = pg_db_factory()
    try:
        return (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_target_id == session_id)
            .order_by(TaskLog.id.desc())
            .one()
        )
    finally:
        verify_db.close()


def _session_status(pg_db_factory: SessionFactory, session_id: int) -> str:
    verify_db = pg_db_factory()
    try:
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        return session.analysis_status
    finally:
        verify_db.close()


def _event_count(pg_db_factory: SessionFactory, session_id: int) -> int:
    verify_db = pg_db_factory()
    try:
        return verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count()
    finally:
        verify_db.close()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def test_frame_budget_limit_zero_off_analyses_all_files(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    """limit=0 (default off): 3 files x 10 frames -> all 3 analysed, no budget key."""
    frame_port = FakeFramePort()
    session_id = _run_analysis(
        pg_db_factory,
        tmp_path,
        frame_port=frame_port,
        pinned_frames=10,
        limit=0,
        file_count=3,
    )

    # One sampler call and one LLM call per file; every file got its plan.
    assert len(frame_port.calls) == 3
    assert len(_latest_gateway().calls) == 3

    task_log = _latest_task_log(pg_db_factory, session_id)
    assert task_log.status == TaskStatus.SUCCESS
    assert "frame_budget_exhausted" not in (task_log.detail_json or {})
    # The single frame_plan key is still recorded (last file's plan, untouched
    # by the budget logic).
    assert "frame_plan" in (task_log.detail_json or {})

    assert _session_status(pg_db_factory, session_id) == "success"
    assert _event_count(pg_db_factory, session_id) == 3


def test_frame_budget_limit_25_analyses_two_of_three_files(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    """limit=25 with 3 x 10-frame files: exactly 2 analysed, the third skipped.

    10 + 10 = 20 <= 25, but 20 + 10 = 30 > 25, so the loop stops before the
    third file: the sampler is called exactly twice and the LLM exactly
    twice. The session ends PARTIAL (never FAILED — deliberate stop) with
    the two analysed files' events kept.
    """
    frame_port = FakeFramePort(elapsed_ms=42)
    session_id = _run_analysis(
        pg_db_factory,
        tmp_path,
        frame_port=frame_port,
        pinned_frames=10,
        limit=25,
        file_count=3,
    )

    # The sampler was called exactly twice (file 3 never sampled), and the
    # LLM gateway exactly twice.
    assert len(frame_port.calls) == 2
    assert len(_latest_gateway().calls) == 2

    # The actual frame count acquired (2 files x 10) is what the budget
    # charges, not the planned count.
    assert sum(len(result.frames) for result in frame_port.results) == 20

    task_log = _latest_task_log(pg_db_factory, session_id)
    assert task_log.status == TaskStatus.SUCCESS
    detail = task_log.detail_json or {}
    assert detail["frame_budget_exhausted"] == {
        "limit": 25,
        "frames_acquired": 20,
        "files_analyzed": 2,
        "files_skipped": 1,
    }
    # The single frame_plan key survives the merge, un-clobbered: it is the
    # LAST analysed file's plan (the pre-sampler write overwrites one key per
    # file — the existing single-key behaviour; no per-file history exists),
    # with the post-sampler fill of the second file.
    frame_plan = detail["frame_plan"]
    assert frame_plan["frames"] == 10
    assert frame_plan["sampler_ms"] == 42
    assert frame_plan["frame_source"] == "sampler"
    assert frame_plan["tokens_assumption"] == "1440p_box_16x9"

    assert _session_status(pg_db_factory, session_id) == "partial"
    # Events of the already-analysed files are kept.
    assert _event_count(pg_db_factory, session_id) == 2
    # The skipped file left no checkpoint behind.
    verify_db = pg_db_factory()
    try:
        checkpoints = (
            verify_db.query(SessionAnalysisCheckpoint)
            .filter(SessionAnalysisCheckpoint.session_id == session_id)
            .order_by(SessionAnalysisCheckpoint.sub_chunk_index)
            .all()
        )
        assert [checkpoint.state for checkpoint in checkpoints] == ["success", "success"]
    finally:
        verify_db.close()


def test_frame_budget_limit_smaller_than_single_file_immediately_exhausted(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    """limit=10 with a file planning 15: zero LLM calls, PARTIAL, detail explains.

    Provider validation rejects ``min_frames`` the model budget cannot afford
    at save time; the analysis-time backstop treats a single file whose plan
    exceeds the whole budget as an immediate exhausted state — no sampler
    call, no LLM call, no checkpoint, PARTIAL session.
    """
    frame_port = FakeFramePort()
    session_id = _run_analysis(
        pg_db_factory,
        tmp_path,
        frame_port=frame_port,
        pinned_frames=15,
        limit=10,
        file_count=1,
    )

    # Neither the sampler nor the LLM was ever called.
    assert frame_port.calls == []
    assert _latest_gateway().calls == []

    task_log = _latest_task_log(pg_db_factory, session_id)
    assert task_log.status == TaskStatus.SUCCESS
    detail = task_log.detail_json or {}
    assert detail["frame_budget_exhausted"] == {
        "limit": 10,
        "frames_acquired": 0,
        "files_analyzed": 0,
        "files_skipped": 1,
    }
    # The stop happens before the pre-sampler transaction, so no frame_plan
    # was persisted at all.
    assert "frame_plan" not in detail

    assert _session_status(pg_db_factory, session_id) == "partial"
    assert _event_count(pg_db_factory, session_id) == 0
    verify_db = pg_db_factory()
    try:
        checkpoints = (
            verify_db.query(SessionAnalysisCheckpoint)
            .filter(SessionAnalysisCheckpoint.session_id == session_id)
            .all()
        )
        assert checkpoints == []
    finally:
        verify_db.close()


def test_frame_budget_detail_keeps_frame_plan_of_last_analysed_file(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    """The budget detail merge never clobbers the analysed files' frame_plan.

    Each file's pre-sampler write overwrites the single
    ``detail_json["frame_plan"]`` key (todo 33's single-key behaviour — the
    last analysed file's plan wins, there is no per-file history); the
    ``frame_budget_exhausted`` merge must preserve that key alongside the
    new one. A 5-file run with a limit of 30 keeps files 1-3 (10+10+10=30
    fits exactly, the budget is inclusive) and skips files 4-5.
    """
    frame_port = FakeFramePort(elapsed_ms=7)
    session_id = _run_analysis(
        pg_db_factory,
        tmp_path,
        frame_port=frame_port,
        pinned_frames=10,
        limit=30,
        file_count=5,
    )

    assert len(frame_port.calls) == 3
    assert len(_latest_gateway().calls) == 3

    task_log = _latest_task_log(pg_db_factory, session_id)
    detail = task_log.detail_json or {}
    assert detail["frame_budget_exhausted"] == {
        "limit": 30,
        "frames_acquired": 30,
        "files_analyzed": 3,
        "files_skipped": 2,
    }
    # The frame_plan key is the THIRD (last analysed) file's plan: same
    # pinned count, but the post-sampler fill carries this run's sampler_ms.
    frame_plan = detail["frame_plan"]
    assert frame_plan["frames"] == 10
    assert frame_plan["sampler_ms"] == 7
    assert frame_plan["frame_source"] == "sampler"

    assert _session_status(pg_db_factory, session_id) == "partial"
    assert _event_count(pg_db_factory, session_id) == 3
