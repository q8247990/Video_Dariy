"""Wave B (todo 10) — analyzer orchestration acquires a FrameSet per file in image mode.

Drives :func:`src.tasks.analyzer.analyze_session_task` end-to-end against
real PostgreSQL (``pg_db_factory``) with a scripted
:class:`~src.application.bootstrap_fakes.ScriptedVisionGateway` and a fake
``frame_set`` port (the :class:`AnalysisPorts` seam). The real
:func:`src.services.analysis.plan.build_analysis_plan` is used as the
``plan_analysis`` binding so the seeded ``VideoFile.duration_seconds``
provenance (``"probe"`` vs ``"fallback"``) flows through
:func:`src.services.session_analysis_video.build_file_sub_chunks` into the
``TaskLog.detail_json["frame_plan"]`` payload.

Covers:

* image mode — the frame port is called exactly once per file, the gateway
  receives ``2*N + 1`` interleaved parts, and ``frame_plan`` records every
  budget key with ``tokens_assumption == "1440p_box_16x9"`` and a filled
  ``sampler_ms``;
* sampler failure — no LLM call, checkpoint ``error`` + session ``partial``,
  zero events, and the pre-sampler ``frame_plan`` survives (``sampler_ms``
  still null);
* ``duration_source == "fallback"`` for a file whose ``duration_seconds``
  is NULL;
* video mode — the frame port is never called, the payload keeps the
  ``video_url`` part + ``num_frames`` hint, and ``detail_json`` has no
  ``frame_plan`` key (regression pin for the byte-for-byte video path);
* image-mode resume — a re-run only re-charges the failed sub-chunk.
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
from src.application.prompt.compiler import compile_video_recognition_prompt
from src.application.prompt.contracts import (
    SessionPromptContext,
    StrategyPromptContext,
    VideoRecognitionPromptInput,
    VideoSourcePromptContext,
)
from src.core.pipeline_constants import TaskStatus
from src.infrastructure.frame_sampler import SamplerUnavailable
from src.models.event_record import EventRecord
from src.models.llm_provider import LLMProvider
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.analysis.plan import build_analysis_plan
from src.services.frame_sampling.budget import (
    compute_frame_plan,
    conservative_tokens_per_image,
    estimate_prompt_tokens,
)
from src.services.frame_sampling.policy import parse_frame_policy
from src.services.frame_sampling.types import FrameSample, FrameSet
from src.services.home_profile import build_home_context
from src.services.prompt_builder.v2.video_recognition import build_strategy_note
from src.services.video_analysis.enums import VIDEO_EVENT_TYPES
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks import _container as _ports_holder
from src.tasks.analyzer import analyze_session_task

SessionFactory = Callable[[], Session]

MAX_MODEL_LEN = 16384
MAX_OUTPUT_TOKENS = 2048
MAX_EDGE = 640


def _is_sha256_hex(value: str) -> bool:
    """The stored ``input_fingerprint`` is a bare sha256 hex digest.

    The mode namespace (``video:`` / ``frames:``) is applied to the frame
    fingerprint *before* it is folded into the sub-chunk fingerprint payload
    and hashed — so the stored value is always a 64-char hex, never prefixed.
    """
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFramePort:
    """Deterministic ``frame_set`` port for the image-input path.

    Frames are derived from the plan's timestamps (one
    :class:`FrameSample` per timestamp, deterministic JPEG bytes) so the
    same file + plan always yields the same frame set — hence the same
    checkpoint fingerprint across runs, which is what the resume tests
    rely on. ``fail_with`` simulates the sampler being unreachable
    (:class:`SamplerUnavailable` propagates unchanged — no fallback).
    """

    def __init__(
        self,
        *,
        fail_with: BaseException | None = None,
        elapsed_ms: int = 42,
        source: str = "sampler",
    ) -> None:
        self.calls: list[tuple[Any, str, Any]] = []
        self.results: list[FrameSet] = []
        self.fail_with = fail_with
        self.elapsed_ms = elapsed_ms
        self.source = source

    def __call__(self, provider: Any, video_path: str, plan: Any) -> FrameSet:
        self.calls.append((provider, video_path, plan))
        if self.fail_with is not None:
            raise self.fail_with
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

    Mirrors ``test_analyzer_task._base_action``; each test installs its own
    :class:`FakeAnalysisPorts` bundle (video- or image-mode shaped).
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


def _seed_provider(db: Session, *, image_mode: bool, max_frames: int = 12) -> LLMProvider:
    """Seed the default vision provider (video mode, or image mode with budget)."""
    extra_config_json: dict[str, Any] | None = None
    if image_mode:
        extra_config_json = {
            "frame_sampling": {
                "mode": "event",
                "min_frames": 2,
                "max_frames": max_frames,
                "probe_fps": 2.0,
                "max_edge": MAX_EDGE,
                "target_context_ratio": 0.9,
            },
            "max_model_len": MAX_MODEL_LEN,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
    provider = LLMProvider(
        provider_name="frame-plan-test",
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
    files: list[tuple[str, int | None]],
) -> int:
    """Seed source + sealed session + one video file per ``(path, duration)``."""
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

    for sort_index, (file_path, duration_seconds) in enumerate(files):
        video_file = VideoFile(
            source_id=source.id,
            file_name=Path(file_path).name,
            file_path=file_path,
            start_time=datetime(2026, 3, 15, 10, 0, 0),
            end_time=datetime(2026, 3, 15, 10, 1, 0),
            duration_seconds=duration_seconds,
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


def _install_ports(
    *,
    use_real_plan: bool,
    frame_port: FakeFramePort | None = None,
    video_url_calls: list[str] | None = None,
) -> FakeAnalysisPorts:
    """Install the ports bundle: scripted media / frame ports.

    ``use_real_plan`` swaps ``plan_analysis`` to the real
    :func:`build_analysis_plan` (image-mode tests seed real
    ``VideoFile`` rows so the ``duration_source`` provenance flows through
    the real planning path); the video-mode regression test keeps the
    deterministic :class:`FakeAnalysisPorts` plan (1 chunk x 3 sub-chunks).
    """
    ports = FakeAnalysisPorts(chunks=1, sub_chunks_per_chunk=3 if not use_real_plan else 1)
    if use_real_plan:
        ports.plan_analysis = build_analysis_plan
    if frame_port is not None:
        ports.frame_set = frame_port
    if video_url_calls is not None:
        original = ports.video_data_url

        def _counting(path: str) -> str:
            video_url_calls.append(path)
            return original(path)

        ports.video_data_url = _counting
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


def _compiler_prompts(
    db: Session,
    session_id: int,
    *,
    sub_chunk_index: int = 0,
    start_offset_seconds: int = 0,
    duration_seconds: int = 60,
    input_mode: str = "video",
    frame_timestamps: list[float] | None = None,
) -> tuple[str, str]:
    """Render the expected prompts through the public compiler (no DB writes)."""
    session = db.query(VideoSession).filter(VideoSession.id == session_id).one()
    source = db.query(VideoSource).filter(VideoSource.id == session.source_id).one()
    home_context = build_home_context(db)
    ingest_type = (
        "xiaomi_nas_backup" if source.source_type == "local_directory" else source.source_type
    )
    return compile_video_recognition_prompt(
        VideoRecognitionPromptInput(
            home_context=home_context,
            video_source=VideoSourcePromptContext(
                source_name=source.source_name,
                camera_name=source.camera_name,
                location_name=source.location_name,
                prompt_text=source.prompt_text,
                source_type=source.source_type,
            ),
            session_context=SessionPromptContext(
                session_id=session.id,
                source_id=session.source_id,
                session_start_time=session.session_start_time,
                session_end_time=session.session_end_time,
                total_duration_seconds=session.total_duration_seconds,
                segment_index=sub_chunk_index,
                segment_start_offset_sec=start_offset_seconds,
                segment_duration_seconds=duration_seconds,
            ),
            strategy_context=StrategyPromptContext(
                ingest_type=ingest_type,
                source_type=source.source_type,
                strategy_note=build_strategy_note(
                    ingest_type=ingest_type, source_type=source.source_type
                ),
            ),
            event_type_list=sorted(VIDEO_EVENT_TYPES),
            input_mode=input_mode,
            frame_timestamps=list(frame_timestamps or []),
        )
    )


def _write_video_file(path: Path, name: str, payload: bytes = b"\x00\x01fake-mp4-bytes") -> str:
    file_path = path / name
    file_path.write_bytes(payload)
    return str(file_path)


# ---------------------------------------------------------------------------
# Budget helper unit tests (no DB)
# ---------------------------------------------------------------------------


def test_conservative_tokens_per_image_is_1440p_box_upper_bound() -> None:
    # 16:9 box bound: (max_edge // 32) * ((max_edge * 9 // 16) // 32)
    assert conservative_tokens_per_image(max_edge=MAX_EDGE) == (MAX_EDGE // 32) * (
        (MAX_EDGE * 9 // 16) // 32
    )
    assert conservative_tokens_per_image(max_edge=2560) == 3600
    assert conservative_tokens_per_image(max_edge=320) == 50
    # one tile wide, less than one 16:9 row -> still >= 1
    assert conservative_tokens_per_image(max_edge=32) == 1
    with pytest.raises(ValueError):
        conservative_tokens_per_image(max_edge=31)  # below one tile edge
    with pytest.raises(ValueError):
        conservative_tokens_per_image(max_edge=0)
    with pytest.raises(ValueError):
        conservative_tokens_per_image(max_edge=True)  # type: ignore[arg-type]


def test_estimate_prompt_tokens_is_conservative_and_never_zero() -> None:
    assert estimate_prompt_tokens() == 800
    assert estimate_prompt_tokens("hi") == 800
    assert estimate_prompt_tokens("x" * 2000) == 2000
    assert estimate_prompt_tokens("a" * 100, "b" * 100) == 800
    assert estimate_prompt_tokens("a" * 2000, "b" * 100) == 2100
    with pytest.raises(ValueError):
        estimate_prompt_tokens(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Image mode
# ---------------------------------------------------------------------------


def test_image_mode_records_frame_plan_and_sends_interleaved_frames(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    frame_port = FakeFramePort(elapsed_ms=42)
    video_url_calls: list[str] = []
    _install_ports(use_real_plan=True, frame_port=frame_port, video_url_calls=video_url_calls)

    file_path = _write_video_file(tmp_path, "cam1.mp4")
    db = pg_db_factory()
    try:
        _seed_provider(db, image_mode=True)
        session_id = _seed_source_session(db, files=[(file_path, 60)])
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(0))
    result = analyze_session_task.run(session_id=session_id)
    assert result["events_created"] == 1

    # The frame port is called exactly once per file (one file here).
    assert len(frame_port.calls) == 1
    _provider, path, plan = frame_port.calls[0]
    assert path == file_path

    # The gateway received 2*N+1 interleaved parts, no video part, no hint.
    gateway = _latest_gateway()
    assert len(gateway.calls) == 1
    content = gateway.calls[0]["messages"][1]["content"]
    assert len(content) == 2 * plan.frames + 1
    assert [item["type"] for item in content] == (["text", "image_url"] * plan.frames) + ["text"]
    assert gateway.calls[0]["extra_body"] == {}

    # No video data URL was ever built in image mode.
    assert video_url_calls == []

    verify_db = pg_db_factory()
    try:
        task_log = (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_target_id == session_id)
            .order_by(TaskLog.id.desc())
            .one()
        )
        frame_plan = task_log.detail_json["frame_plan"]
        # Exact budget keys and values.
        assert frame_plan["context_size"] == MAX_MODEL_LEN
        assert frame_plan["max_model_len"] == MAX_MODEL_LEN
        assert frame_plan["tokens_per_image"] == conservative_tokens_per_image(max_edge=MAX_EDGE)
        assert frame_plan["tokens_per_image"] == plan.tokens_per_image
        assert frame_plan["tokens_assumption"] == "1440p_box_16x9"
        assert isinstance(frame_plan["prompt_tokens_est"], int)
        assert frame_plan["prompt_tokens_est"] >= 800
        assert frame_plan["max_output_tokens"] == MAX_OUTPUT_TOKENS
        # Single-resolver invariant: the request's max_tokens is the same
        # number the frame-budget formula subtracted (2048 here, not 8192).
        assert gateway.calls[0]["max_tokens"] == frame_plan["max_output_tokens"]
        # The recorded frame count is the plan's and equals the frame set
        # the sampler actually returned.
        assert frame_plan["frames"] == plan.frames == 12
        assert frame_plan["frames"] == len(frame_port.results[0].frames)
        assert frame_plan["clamp_reason"] == "max"
        assert frame_plan["duration_seconds"] == 60
        assert frame_plan["duration_source"] == "probe"
        # Post-sampler fill.
        assert frame_plan["sampler_ms"] == 42
        assert frame_plan["frame_source"] == "sampler"

        checkpoint = (
            verify_db.query(SessionAnalysisCheckpoint)
            .filter(SessionAnalysisCheckpoint.session_id == session_id)
            .one()
        )
        assert checkpoint.state == "success"
        assert _is_sha256_hex(checkpoint.input_fingerprint)
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert session.analysis_status == "success"
    finally:
        verify_db.close()


def test_image_mode_prompt_contains_frame_list_and_image_rules(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    """The orchestration layer must SEND the image-mode prompt, not only build it.

    Regression for the spike-49 defect: ``_build_prompts`` used to omit
    ``input_mode`` / ``frame_timestamps``, so the gateway received the video
    branch (no timestamp list, video rules) even though the payload was
    interleaved images. The assertions read the scripted gateway's recorded
    ``messages`` — i.e. the exact request body — after a full task run.
    """
    frame_port = FakeFramePort()
    _install_ports(use_real_plan=True, frame_port=frame_port)

    file_path = _write_video_file(tmp_path, "cam1.mp4")
    db = pg_db_factory()
    try:
        _seed_provider(db, image_mode=True)
        session_id = _seed_source_session(db, files=[(file_path, 60)])
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(0))
    analyze_session_task.run(session_id=session_id)

    frames = frame_port.results[0].frames
    gateway = _latest_gateway()
    assert len(gateway.calls) == 1
    system_prompt = gateway.calls[0]["messages"][0]["content"]
    user_content = gateway.calls[0]["messages"][1]["content"]
    user_prompt = user_content[-1]["text"]

    # system prompt is the image branch, not the video branch.
    assert "5. 事件粒度：优先输出多个短事件，只有同一持续行为才合并。" in system_prompt
    assert (
        "6. 时间规则：offset_start_sec / offset_end_sec 以本批第 0 帧为 0 秒，"
        "且必须对齐到给定帧时间戳。" in system_prompt
    )
    assert "17. 事件数量上限：每个文件最多输出 8 个事件" in system_prompt
    assert "连续同类活动尽量合并" not in system_prompt

    # user prompt carries the REAL timestamps of the frames sent.
    assert "本批帧时间戳：" in user_prompt
    timestamp_lines = [line for line in user_prompt.splitlines() if line.startswith("帧 ")]
    assert timestamp_lines == [
        f"帧 {index}/{len(frames)} — t={frame.t_seconds:.1f}s"
        for index, frame in enumerate(frames, start=1)
    ]
    # The interleaved payload labels and the prompt list agree line by line.
    labels = [item["text"] for item in user_content if item.get("type") == "text"][:-1]
    assert labels == timestamp_lines

    verify_db = pg_db_factory()
    try:
        task_log = (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_target_id == session_id)
            .order_by(TaskLog.id.desc())
            .one()
        )
        frame_plan = task_log.detail_json["frame_plan"]
        video_system, video_user = _compiler_prompts(verify_db, session_id)
    finally:
        verify_db.close()

    # The recorded estimate covers the frame list, not just the video prompt.
    assert frame_plan["prompt_tokens_est"] == estimate_prompt_tokens(system_prompt, user_prompt)
    assert frame_plan["prompt_tokens_est"] > estimate_prompt_tokens(video_system, video_user)


def test_image_mode_frame_plan_converges_with_frame_list_budget(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    """The budget plan must stay conservative with the frame list it actually sends.

    With a large ``max_frames`` the budget (not the policy ceiling) decides
    the count. The frame-list block itself consumes prompt tokens, so the
    first plan (estimated without / with a short list) is larger than the
    plan re-rendered with its own frame count. This test pins the convergence
    invariant: the recorded plan never acquires more frames than the prompt
    actually sent can afford, and recomputing with that prompt's estimate
    yields at most one additional frame (the plan may be one frame
    conservative, never over-provisioned).
    """
    frame_port = FakeFramePort()
    _install_ports(use_real_plan=True, frame_port=frame_port)

    file_path = _write_video_file(tmp_path, "cam1.mp4")
    db = pg_db_factory()
    try:
        _seed_provider(db, image_mode=True, max_frames=60)
        session_id = _seed_source_session(db, files=[(file_path, 60)])
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(0))
    analyze_session_task.run(session_id=session_id)

    gateway = _latest_gateway()
    assert len(gateway.calls) == 1
    system_prompt = gateway.calls[0]["messages"][0]["content"]
    user_prompt = gateway.calls[0]["messages"][1]["content"][-1]["text"]
    final_prompt_tokens_est = estimate_prompt_tokens(system_prompt, user_prompt)

    verify_db = pg_db_factory()
    try:
        task_log = (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_target_id == session_id)
            .order_by(TaskLog.id.desc())
            .one()
        )
        frame_plan = task_log.detail_json["frame_plan"]
        provider = (
            verify_db.query(LLMProvider).filter(LLMProvider.is_default_vision.is_(True)).one()
        )
        policy = parse_frame_policy(provider)
        video_system, video_user = _compiler_prompts(verify_db, session_id)
    finally:
        verify_db.close()

    frames = frame_port.results[0].frames
    timestamp_lines = [line for line in user_prompt.splitlines() if line.startswith("帧 ")]
    assert len(timestamp_lines) == frame_plan["frames"]
    assert frame_plan["clamp_reason"] == "budget"
    assert frame_plan["frames"] == len(frames)
    assert frame_plan["frames"] < 60
    # The plan estimate is rendered with the previous convergence round's
    # frame count, so the prompt actually sent may estimate up to one frame
    # smaller; the plan must stay conservative (never over-provisioned).
    assert (
        abs(frame_plan["prompt_tokens_est"] - final_prompt_tokens_est)
        < (frame_plan["tokens_per_image"])
    )
    # The frame list really moved the budget: the plan that ignores it is larger.
    plan_without_frame_list = compute_frame_plan(
        context_size=frame_plan["context_size"],
        policy=policy,
        duration_seconds=frame_plan["duration_seconds"],
        prompt_tokens_est=estimate_prompt_tokens(video_system, video_user),
        frame_width=policy.max_edge,
        frame_height=policy.max_edge * 9 // 16,
        max_output_tokens=frame_plan["max_output_tokens"],
        duration_source=frame_plan["duration_source"],
    )
    assert plan_without_frame_list.frames > frame_plan["frames"]
    recomputed = compute_frame_plan(
        context_size=frame_plan["context_size"],
        policy=policy,
        duration_seconds=frame_plan["duration_seconds"],
        prompt_tokens_est=frame_plan["prompt_tokens_est"],
        frame_width=policy.max_edge,
        frame_height=policy.max_edge * 9 // 16,
        max_output_tokens=frame_plan["max_output_tokens"],
        duration_source=frame_plan["duration_source"],
    )
    assert recomputed.frames == frame_plan["frames"]
    recomputed_from_sent_prompt = compute_frame_plan(
        context_size=frame_plan["context_size"],
        policy=policy,
        duration_seconds=frame_plan["duration_seconds"],
        prompt_tokens_est=final_prompt_tokens_est,
        frame_width=policy.max_edge,
        frame_height=policy.max_edge * 9 // 16,
        max_output_tokens=frame_plan["max_output_tokens"],
        duration_source=frame_plan["duration_source"],
    )
    assert recomputed_from_sent_prompt.frames == frame_plan["frames"] or (
        recomputed_from_sent_prompt.frames == frame_plan["frames"] + 1
    )
    # The post-acquire guard's inequality holds for the real request.
    assert (
        final_prompt_tokens_est
        + frame_plan["max_output_tokens"]
        + len(frames) * frame_plan["tokens_per_image"]
        <= frame_plan["max_model_len"]
    )


def test_image_mode_sampler_failure_keeps_frame_plan_and_marks_partial(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    frame_port = FakeFramePort(fail_with=SamplerUnavailable("sampler service down"))
    _install_ports(use_real_plan=True, frame_port=frame_port)

    file_path = _write_video_file(tmp_path, "cam1.mp4")
    db = pg_db_factory()
    try:
        _seed_provider(db, image_mode=True)
        session_id = _seed_source_session(db, files=[(file_path, 60)])
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(0))
    with pytest.raises(SamplerUnavailable):
        analyze_session_task.run(session_id=session_id)

    # The sampler was attempted once; the LLM was never called.
    assert len(frame_port.calls) == 1
    assert _latest_gateway().calls == []

    verify_db = pg_db_factory()
    try:
        checkpoint = (
            verify_db.query(SessionAnalysisCheckpoint)
            .filter(SessionAnalysisCheckpoint.session_id == session_id)
            .one()
        )
        assert checkpoint.state == "error"
        assert checkpoint.error_type == "SamplerUnavailable"
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert session.analysis_status == "partial"
        assert (
            verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 0
        )
        task_log = (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_target_id == session_id)
            .order_by(TaskLog.id.desc())
            .one()
        )
        assert task_log.status == TaskStatus.FAILED
        # The pre-sampler detail survives the failure path with sampler_ms null.
        frame_plan = task_log.detail_json["frame_plan"]
        assert frame_plan["sampler_ms"] is None
        assert "frame_source" not in frame_plan
        assert frame_plan["tokens_assumption"] == "1440p_box_16x9"
        assert frame_plan["frames"] >= 2
    finally:
        verify_db.close()


def test_image_mode_fallback_duration_source_recorded(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    """A NULL ``VideoFile.duration_seconds`` yields ``duration_source == "fallback"``."""
    frame_port = FakeFramePort()
    _install_ports(use_real_plan=True, frame_port=frame_port)

    file_path = _write_video_file(tmp_path, "cam1.mp4")
    db = pg_db_factory()
    try:
        _seed_provider(db, image_mode=True)
        session_id = _seed_source_session(db, files=[(file_path, None)])
        db.commit()
    finally:
        db.close()

    _set_responses(_recognition_result_json(0))
    analyze_session_task.run(session_id=session_id)

    verify_db = pg_db_factory()
    try:
        task_log = (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_target_id == session_id)
            .order_by(TaskLog.id.desc())
            .one()
        )
        frame_plan = task_log.detail_json["frame_plan"]
        assert frame_plan["duration_source"] == "fallback"
        # The fallback duration is the seeded 60 s span (start 10:00 -> end 10:01).
        assert frame_plan["duration_seconds"] == 60
    finally:
        verify_db.close()


def test_image_mode_resume_recharges_only_failed_sub_chunk(
    pg_db_factory: SessionFactory,
    tmp_path: Path,
) -> None:
    frame_port = FakeFramePort()
    _install_ports(use_real_plan=True, frame_port=frame_port)

    file_a = _write_video_file(tmp_path, "cam1.mp4")
    file_b = _write_video_file(tmp_path, "cam2.mp4")
    db = pg_db_factory()
    try:
        _seed_provider(db, image_mode=True)
        session_id = _seed_source_session(db, files=[(file_a, 60), (file_b, 60)])
        db.commit()
    finally:
        db.close()

    # Run 1: file A succeeds, file B's LLM call raises.
    _set_responses(_recognition_result_json(0), "raise")
    with pytest.raises(RuntimeError, match="provider failed"):
        analyze_session_task.run(session_id=session_id)
    assert len(frame_port.calls) == 2

    # Run 2: only the failed sub-chunk calls the LLM again.
    _set_responses(_recognition_result_json(1))
    result = analyze_session_task.run(session_id=session_id)
    assert result["events_created"] == 2
    assert len(_latest_gateway().calls) == 1

    verify_db = pg_db_factory()
    try:
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert session.analysis_status == "success"
        checkpoints = (
            verify_db.query(SessionAnalysisCheckpoint)
            .filter(SessionAnalysisCheckpoint.session_id == session_id)
            .order_by(SessionAnalysisCheckpoint.sub_chunk_index)
            .all()
        )
        assert [checkpoint.state for checkpoint in checkpoints] == ["success", "success"]
    finally:
        verify_db.close()


# ---------------------------------------------------------------------------
# Video mode — regression pin
# ---------------------------------------------------------------------------


def test_video_mode_does_not_call_frame_port_or_record_frame_plan(
    pg_db_factory: SessionFactory,
) -> None:
    frame_port = FakeFramePort()
    video_url_calls: list[str] = []
    _install_ports(use_real_plan=False, frame_port=frame_port, video_url_calls=video_url_calls)

    db = pg_db_factory()
    try:
        _seed_provider(db, image_mode=False)
        session_id = _seed_source_session(db, files=[])
        db.commit()
    finally:
        db.close()

    # Default FakeAnalysisPorts plan: 1 chunk x 3 sub-chunks.
    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        _recognition_result_json(2),
    )
    result = analyze_session_task.run(session_id=session_id)
    assert result["events_created"] == 3

    # The frame port is never called in video mode.
    assert frame_port.calls == []
    # The video data URL is built per file (3 sub-chunks).
    assert len(video_url_calls) == 3

    gateway = _latest_gateway()
    assert len(gateway.calls) == 3
    for call in gateway.calls:
        content = call["messages"][1]["content"]
        assert [item["type"] for item in content] == ["video_url", "text"]
        assert content[0]["video_url"]["url"] == "data:video/mp4;base64,AAAA"
        assert call["extra_body"] == {"media_io_kwargs": {"video": {"num_frames": 120}}}

    verify_db = pg_db_factory()
    try:
        task_log = (
            verify_db.query(TaskLog)
            .filter(TaskLog.task_target_id == session_id)
            .order_by(TaskLog.id.desc())
            .one()
        )
        assert "frame_plan" not in (task_log.detail_json or {})
        checkpoints = verify_db.query(SessionAnalysisCheckpoint).all()
        assert len(checkpoints) == 3
        for checkpoint in checkpoints:
            assert _is_sha256_hex(checkpoint.input_fingerprint)
    finally:
        verify_db.close()


def test_video_mode_prompt_is_byte_identical_to_compiler_defaults(
    pg_db_factory: SessionFactory,
) -> None:
    """Video mode keeps rendering the compiler's default (video) branch.

    Snapshot pin for the raw_mp4 path: the scripts the gateway received must
    equal ``compile_video_recognition_prompt`` called with the contract
    defaults (``input_mode="video"`` / ``frame_timestamps=[]``), byte for
    byte, including the video-only rule wording and no frame list.
    """
    _install_ports(use_real_plan=False)

    db = pg_db_factory()
    try:
        _seed_provider(db, image_mode=False)
        session_id = _seed_source_session(db, files=[])
        db.commit()
        expected_prompts = [
            _compiler_prompts(
                db,
                session_id,
                sub_chunk_index=index,
                start_offset_seconds=index * 60,
            )
            for index in range(3)
        ]
    finally:
        db.close()

    _set_responses(
        _recognition_result_json(0),
        _recognition_result_json(1),
        _recognition_result_json(2),
    )
    analyze_session_task.run(session_id=session_id)

    gateway = _latest_gateway()
    assert len(gateway.calls) == 3
    for call, (expected_system, expected_user) in zip(gateway.calls, expected_prompts, strict=True):
        assert call["messages"][0]["content"] == expected_system
        assert call["messages"][1]["content"][-1]["text"] == expected_user
        assert "连续同类活动尽量合并" in expected_system
        assert "本批帧时间戳" not in expected_user
        assert "优先输出多个短事件" not in expected_system
