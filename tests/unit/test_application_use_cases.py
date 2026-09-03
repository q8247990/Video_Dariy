"""Wave 1 / Todo 6 use case unit tests.

These tests prove the contract of every use case introduced under Todo
6 — ``stop_task_log_use_case`` / ``retry_task_log_use_case`` (Task
cancel/retry), ``generate_entity_appearance_use_case`` (Home Profile
vision) and the QA / MCP ``AnswerQuestionUseCase`` /
``MCPToolUseCase`` wrappers. They drive the use cases against a
:class:`Container` built by ``bootstrap_for_tests`` so the tests stay
hermetic — no monkeypatching, no Celery / Redis / SQLAlchemy fixtures.

The behaviour asserted here is identical to the previous endpoint
implementation; the refactor only changed the call site.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pytest
from sqlalchemy.orm import Session

from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import (
    FakeLLMGatewayFactory,
    FakeTaskControl,
    FakeTaskDispatcher,
)
from src.application.home_profile import generate_entity_appearance_use_case
from src.application.mcp.use_case_tools import MCPToolUseCase
from src.application.qa.schemas import QARequest
from src.application.qa.use_case_query import AnswerQuestionUseCase
from src.application.tasks import (
    retry_task_log_use_case,
    stop_task_log_use_case,
)
from src.models.home_entity_profile import HomeEntityProfile
from src.models.llm_provider import LLMProvider
from src.models.task_log import TaskLog
from src.models.video_source import VideoSource
from src.services.pipeline_constants import TaskStatus, TaskType

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_task_log(
    db: Session,
    *,
    status: str,
    queue_task_id: Optional[str] = None,
    task_type: str = TaskType.SESSION_BUILD,
    task_target_id: Optional[int] = None,
) -> TaskLog:
    log = TaskLog(
        task_type=task_type,
        task_target_id=task_target_id,
        queue_task_id=queue_task_id,
        status=status,
    )
    db.add(log)
    db.flush()
    return log


# ---------------------------------------------------------------------------
# Task cancel use case
# ---------------------------------------------------------------------------


def test_stop_task_log_use_case_invokes_task_control_with_terminate(pg_db: Session) -> None:
    log = _seed_task_log(pg_db, status=TaskStatus.RUNNING, queue_task_id="running-task")
    fake_task_control = FakeTaskControl()
    container = bootstrap_for_tests(task_control=fake_task_control)

    result = stop_task_log_use_case(
        db=pg_db,
        task_log_id=log.id,
        locale="zh-CN",
        container=container,
    )

    assert result.error_code == 0
    assert fake_task_control.revocations == [("running-task", True)]
    assert result.payload["task_log_id"] == log.id
    assert result.payload["cancel_requested"] is True
    assert result.payload["status"] == TaskStatus.RUNNING


def test_stop_task_log_use_case_cancels_pending_without_revoke(pg_db: Session) -> None:
    log = _seed_task_log(pg_db, status=TaskStatus.PENDING, queue_task_id=None)
    fake_task_control = FakeTaskControl()
    container = bootstrap_for_tests(task_control=fake_task_control)

    result = stop_task_log_use_case(
        db=pg_db,
        task_log_id=log.id,
        locale="zh-CN",
        container=container,
    )

    assert result.error_code == 0
    assert fake_task_control.revocations == []  # no queue_task_id ⇒ no revoke
    assert result.payload["status"] == TaskStatus.CANCELLED
    pg_db.commit()
    pg_db.refresh(log)
    assert log.status == TaskStatus.CANCELLED
    assert log.finished_at is not None


def test_stop_task_log_use_case_returns_4002_when_missing(pg_db: Session) -> None:
    container = bootstrap_for_tests()

    result = stop_task_log_use_case(
        db=pg_db,
        task_log_id=9999,
        locale="zh-CN",
        container=container,
    )

    assert result.error_code == 4002
    assert result.payload is None


# ---------------------------------------------------------------------------
# Task retry use case
# ---------------------------------------------------------------------------


def test_retry_task_log_use_case_returns_4002_when_missing(pg_db: Session) -> None:
    container = bootstrap_for_tests()

    result = retry_task_log_use_case(
        db=pg_db,
        task_log_id=424242,
        locale="zh-CN",
        container=container,
    )

    assert result.error_code == 4002
    assert result.task_id is None


def test_retry_task_log_use_case_dispatches_via_container(pg_db: Session) -> None:
    source = VideoSource(
        source_name="客厅",
        camera_name="cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
        last_validate_status="success",
    )
    pg_db.add(source)
    pg_db.flush()
    log = _seed_task_log(
        pg_db,
        status=TaskStatus.FAILED,
        task_type=TaskType.SESSION_BUILD,
        task_target_id=source.id,
    )
    # detail_json with scan_mode=hot matches retry_task validation.
    log.detail_json = {"scan_mode": "hot", "dedupe_key": f"session_build|{source.id}|hot"}
    pg_db.commit()

    fake_dispatcher = FakeTaskDispatcher()
    fake_dispatcher.set_next_return("dispatched-id")
    container = bootstrap_for_tests(dispatcher=fake_dispatcher)

    result = retry_task_log_use_case(
        db=pg_db,
        task_log_id=log.id,
        locale="zh-CN",
        container=container,
    )

    assert result.error_code == 0
    assert result.task_id == "dispatched-id"
    # Dispatcher was called with the session_build command (via the
    # use case's PipelineOrchestrator binding).
    assert len(fake_dispatcher.dispatched_session_build) == 1


# ---------------------------------------------------------------------------
# Home Profile vision use case
# ---------------------------------------------------------------------------


def _seed_entity_with_image(db: Session, *, image_path: str) -> HomeEntityProfile:
    entity = HomeEntityProfile(
        entity_type="pet",
        name="豆豆",
        role_type="cat",
        image_path=image_path,
        appearance_desc="",
    )
    db.add(entity)
    db.flush()
    return entity


class _FakeEntityResponse:
    """Tiny stand-in for ``HomeEntityResponse`` used by the builder."""

    def __init__(self, entity: HomeEntityProfile) -> None:
        self.entity = entity

    def model_dump(self) -> dict[str, Any]:
        return {"id": self.entity.id, "appearance_desc": self.entity.appearance_desc or ""}


def test_generate_entity_appearance_use_case_persists_llm_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, pg_db: Session
) -> None:
    image_path = tmp_path / "entity.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)
    entity = _seed_entity_with_image(pg_db, image_path=str(image_path))

    provider = LLMProvider(
        provider_name="local-vision",
        provider_type="vision_provider",
        api_base_url="http://localhost:8000/v1",
        api_key="",
        model_name="fake-vision",
        timeout_seconds=30,
        enabled=True,
        supports_vision=True,
        is_default_vision=True,
    )
    pg_db.add(provider)
    pg_db.commit()

    factory = FakeLLMGatewayFactory()
    factory.gateways.clear()
    factory.build_calls.clear()

    original_build = factory.build

    def _build(**kwargs: Any) -> Any:
        gateway_ = original_build(**kwargs)
        gateway_.replies.append("豆豆是一只橘色短毛猫")
        return gateway_

    monkeypatch.setattr(factory, "build", _build)
    container = bootstrap_for_tests(llm_factory=factory)

    result = generate_entity_appearance_use_case(
        db=pg_db,
        entity_id=entity.id,
        locale="zh-CN",
        container=container,
        entity_image_path_resolver=lambda eid: str(image_path),
        entity_response_builder=_FakeEntityResponse,  # type: ignore[arg-type]
    )

    assert result.error_code == 0, result.error_message
    assert result.entity is not None
    assert "橘色短毛猫" in result.entity.entity.appearance_desc
    # The factory recorded exactly one build call for the vision provider.
    assert len(factory.build_calls) == 1


def test_generate_entity_appearance_use_case_returns_4002_when_missing(pg_db: Session) -> None:
    container = bootstrap_for_tests()

    result = generate_entity_appearance_use_case(
        db=pg_db,
        entity_id=999,
        locale="zh-CN",
        container=container,
        entity_image_path_resolver=lambda eid: "/nope.jpg",
        entity_response_builder=_FakeEntityResponse,  # type: ignore[arg-type]
    )

    assert result.error_code == 4002
    assert result.entity is None


# ---------------------------------------------------------------------------
# QA / MCP use cases
# ---------------------------------------------------------------------------


class _RecordingQAService:
    """Stand-in for ``QAService`` used to verify the use-case wrapper."""

    instances: list["_RecordingQAService"] = []

    def __init__(self, *, db: Session, llm_factory: Any = None) -> None:
        self.db = db
        self.llm_factory = llm_factory
        _RecordingQAService.instances.append(self)

    def answer(self, request: QARequest) -> Any:  # pragma: no cover - behaviour irrelevant
        from src.application.qa.schemas import QAResult

        return QAResult(
            question=request.question,
            answer_text=f"echo: {request.question}",
            provider_id=None,
        )


def test_answer_question_use_case_binds_llm_factory(
    monkeypatch: pytest.MonkeyPatch, pg_db: Session
) -> None:
    _RecordingQAService.instances.clear()
    fake_factory = FakeLLMGatewayFactory()
    container = bootstrap_for_tests(llm_factory=fake_factory)

    # Patch QAService so the use case is exercised without the
    # full QA plumbing (DB provider rows, token quota, etc).
    from src.application.qa import service as qa_service_module

    monkeypatch.setattr(qa_service_module, "QAService", _RecordingQAService)

    use_case = AnswerQuestionUseCase(db=pg_db, container=container)
    result = use_case.execute(
        QARequest(
            question="hello",
            now=datetime(2026, 9, 2, 12, 0, 0),
        )
    )

    assert result.answer_text == "echo: hello"
    assert len(_RecordingQAService.instances) == 1
    assert _RecordingQAService.instances[0].llm_factory is fake_factory


def test_mcp_tool_use_case_routes_ask_home_monitor(
    monkeypatch: pytest.MonkeyPatch, pg_db: Session
) -> None:
    container = bootstrap_for_tests()
    # Substitute QAService so the ask_home_monitor path returns a
    # known answer without requiring provider rows.
    from src.application.mcp import service as mcp_service

    def _stub(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "answer_text": "on the couch",
            "referenced_events": [],
            "referenced_sessions": [],
        }

    monkeypatch.setattr(mcp_service.MCPToolService, "ask_home_monitor", _stub)

    use_case = MCPToolUseCase(
        db=pg_db,
        container=container,
        stream_url_builder=lambda fid: f"/files/{fid}",
        session_playback_url_builder=lambda sid: f"/sessions/{sid}",
    )

    result = use_case.execute(
        "ask_home_monitor",
        {"question": "where is the cat?"},
        locale="zh-CN",
    )

    assert result["answer_text"] == "on the couch"


def test_mcp_tool_use_case_surfaces_invalid_argument(
    monkeypatch: pytest.MonkeyPatch, pg_db: Session
) -> None:
    from src.application.mcp.service import MCPInvalidArgumentError

    container = bootstrap_for_tests()

    use_case = MCPToolUseCase(
        db=pg_db,
        container=container,
        stream_url_builder=lambda fid: f"/files/{fid}",
        session_playback_url_builder=lambda sid: f"/sessions/{sid}",
    )

    with pytest.raises(MCPInvalidArgumentError):
        use_case.execute("ask_home_monitor", {}, locale="zh-CN")
