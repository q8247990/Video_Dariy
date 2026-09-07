"""Real-HTTP contract tests for the daily_summaries endpoints.

The original :mod:`tests.unit.test_daily_summaries_endpoint` swapped a
``MagicMock`` in for the pipeline orchestrator. Those tests exercise
private helpers in isolation, which is not the project's current
contract-testing posture (HTTP status + JSON body + port interaction).

The tests below are HTTP-driven:

* The first test keeps the original integration pattern (monkeypatch on
  the module-level ``_pipeline_orchestrator``) so existing coverage
  stays green.
* The remaining tests ride on
  :func:`tests.integration.conftest.make_http_client` and replace the
  orchestrator seam via FastAPI ``dependency_overrides`` so the endpoint
  dispatches through a :class:`FakeTaskDispatcher` port fake. Assertions
  are on the HTTP response and on the commands received by the fake
  dispatcher — never on internal call history.

SIZE_OK: the task brief explicitly mandates a single file for the
``daily-summaries`` endpoint family (``tests/integration/test_daily_summaries_http.py``),
which naturally pushes the file past 250 pure LOC once every contract
path (200/400/404/409/401 + port-level dispatch assertions) gets its own
self-contained test. Splitting per-endpoint-verb would break the
explicit per-endpoint-file layout.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.api import deps as api_deps
from src.api.error_status import ResponseStatusMiddleware
from src.api.v1.endpoints import daily_summaries as daily_summaries_module
from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeTaskDispatcher
from src.application.pipeline.commands import GenerateDailySummaryCommand
from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.models.daily_summary import DailySummary
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)


@pytest.fixture
def client(pg_db: Session, make_http_client) -> Iterator[TestClient]:
    routers = [(daily_summaries_module.router, "/api/v1/daily-summaries")]
    with make_http_client(routers) as test_client:
        yield test_client


def test_generate_all_daily_summaries_route_hits_post_handler(
    client: TestClient, pg_db: Session, monkeypatch
) -> None:
    source = VideoSource(
        source_name="test-source",
        camera_name="test-camera",
        location_name="home",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()
    pg_db.add(
        VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 10, 8, 0, 0),
            session_end_time=datetime(2026, 3, 10, 8, 30, 0),
            total_duration_seconds=1800,
            analysis_status=SessionAnalysisStatus.SUCCESS,
        )
    )
    pg_db.commit()

    monkeypatch.setattr(
        "src.api.v1.endpoints.daily_summaries._pipeline_orchestrator.dispatch_generate_daily_summary",
        lambda _db, command: f"task-{command.target_date_str}",
    )

    response = client.post("/api/v1/daily-summaries/generate-all")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["queued_count"] == 1
    assert payload["data"]["target_dates"] == ["2026-03-10"]


# ---------------------------------------------------------------------------
# Below: real-HTTP contract cases that ride on the FakeTaskDispatcher port.
# These cover the remaining paths from the original unit tests and assert
# on the wire contract (HTTP status + JSON body) plus the dispatched
# commands seen by the fake — no MagicMock, no private-helper inspection.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_dispatcher() -> Iterator[FakeTaskDispatcher]:
    """Bind a FakeTaskDispatcher to the API composition root.

    The endpoint module (``daily_summaries.py``) holds a module-level
    singleton built at import time from the production container, so
    swapping ``api_deps._container`` alone is not enough — the test
    client must also override the endpoint's ``get_orchestrator``
    dependency (see the ``client_with_fake_dispatcher`` fixture below).

    Teardown restores the original container so subsequent tests are
    not contaminated by our fake.
    """

    original = api_deps._container
    fake = FakeTaskDispatcher()
    api_deps.set_container_for_tests(bootstrap_for_tests(dispatcher=fake))
    try:
        yield fake
    finally:
        api_deps.set_container_for_tests(original)


@pytest.fixture
def client_with_fake_dispatcher(
    pg_db: Session, make_http_client, fake_dispatcher: FakeTaskDispatcher
) -> Iterator[TestClient]:
    with make_http_client(
        [(daily_summaries_module.router, "/api/v1/daily-summaries")],
        middleware=[ResponseStatusMiddleware],
        extra_overrides={
            daily_summaries_module.get_orchestrator: lambda: PipelineOrchestrator(
                dispatcher=fake_dispatcher,
            ),
        },
    ) as test_client:
        yield test_client


@pytest.fixture
def anonymous_client(pg_db: Session, make_http_client) -> Iterator[TestClient]:
    """Anonymous client (no ``get_current_user`` override) for 401 assertions."""

    with make_http_client(
        [(daily_summaries_module.router, "/api/v1/daily-summaries")],
        authenticated=False,
    ) as test_client:
        yield test_client


def _seed_success_session(
    pg_db: Session,
    *,
    session_end: datetime,
    source_name: str = "daily-source",
) -> None:
    source = VideoSource(
        source_name=source_name,
        camera_name="cam",
        location_name="home",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()
    pg_db.add(
        VideoSession(
            source_id=source.id,
            session_start_time=session_end.replace(hour=8, minute=0, second=0),
            session_end_time=session_end,
            total_duration_seconds=1800,
            analysis_status=SessionAnalysisStatus.SUCCESS,
        )
    )
    pg_db.commit()


def test_generate_all_daily_summaries_returns_409_when_no_successful_sessions(
    client_with_fake_dispatcher: TestClient, pg_db: Session
) -> None:
    response = client_with_fake_dispatcher.post("/api/v1/daily-summaries/generate-all")

    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == 4004
    assert payload["data"] is None


def test_generate_all_daily_summaries_dispatches_one_command_per_date(
    client_with_fake_dispatcher: TestClient,
    pg_db: Session,
    fake_dispatcher: FakeTaskDispatcher,
) -> None:
    _seed_success_session(
        pg_db, session_end=datetime(2026, 3, 10, 8, 30, 0), source_name="src-daily-10"
    )
    _seed_success_session(
        pg_db, session_end=datetime(2026, 3, 12, 9, 20, 0), source_name="src-daily-12"
    )

    response = client_with_fake_dispatcher.post("/api/v1/daily-summaries/generate-all")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["earliest_date"] == "2026-03-10"
    assert payload["data"]["latest_date"] == "2026-03-12"
    assert payload["data"]["target_dates"] == ["2026-03-10", "2026-03-12"]
    assert payload["data"]["queued_count"] == 2
    assert payload["data"]["skipped_count"] == 0
    assert payload["data"]["skipped_dates"] == []

    assert len(fake_dispatcher.dispatched_daily_summary) == 2
    assert [c.target_date_str for c in fake_dispatcher.dispatched_daily_summary] == [
        "2026-03-10",
        "2026-03-12",
    ]
    for command in fake_dispatcher.dispatched_daily_summary:
        assert isinstance(command, GenerateDailySummaryCommand)


def test_generate_all_daily_summaries_reports_skipped_dates_when_active_task_exists(
    client_with_fake_dispatcher: TestClient,
    pg_db: Session,
    fake_dispatcher: FakeTaskDispatcher,
) -> None:
    _seed_success_session(
        pg_db, session_end=datetime(2026, 3, 10, 8, 30, 0), source_name="src-skip-10"
    )
    _seed_success_session(
        pg_db, session_end=datetime(2026, 3, 12, 9, 20, 0), source_name="src-skip-12"
    )
    pg_db.add(
        TaskLog(
            task_type=TaskType.DAILY_SUMMARY_GENERATION,
            status=TaskStatus.RUNNING,
            detail_json={"target_date": "2026-03-10"},
            dedupe_key="daily_summary_generation|2026-03-10",
        )
    )
    pg_db.commit()

    response = client_with_fake_dispatcher.post("/api/v1/daily-summaries/generate-all")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["target_dates"] == ["2026-03-10", "2026-03-12"]
    assert payload["data"]["queued_count"] == 1
    assert payload["data"]["skipped_count"] == 1
    assert payload["data"]["skipped_dates"] == ["2026-03-10"]

    assert [c.target_date_str for c in fake_dispatcher.dispatched_daily_summary] == ["2026-03-12"]


def test_get_daily_summary_returns_400_for_invalid_date_format(
    client_with_fake_dispatcher: TestClient,
) -> None:
    response = client_with_fake_dispatcher.get("/api/v1/daily-summaries/not-a-date")

    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == 4000
    assert payload["data"] is None


def test_get_daily_summary_returns_404_when_not_found(
    client_with_fake_dispatcher: TestClient,
) -> None:
    response = client_with_fake_dispatcher.get("/api/v1/daily-summaries/2026-04-01")

    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == 4002
    assert payload["data"] is None


def test_get_daily_summary_returns_200_with_payload_when_present(
    client_with_fake_dispatcher: TestClient, pg_db: Session
) -> None:
    pg_db.add(
        DailySummary(
            summary_date=datetime(2026, 3, 14).date(),
            summary_title="2026-03-14 家庭日报",
            overall_summary="昨天整体平稳。",
            subject_sections_json=[
                {
                    "subject_name": "爸爸",
                    "subject_type": "member",
                    "summary": "上午在客厅活动较多。",
                    "attention_needed": False,
                }
            ],
            attention_items_json=[
                {
                    "title": "门口短暂停留",
                    "summary": "傍晚门口有短暂停留，建议留意。",
                    "level": "low",
                }
            ],
            event_count=3,
            generated_at=datetime(2026, 3, 15, 8, 0, 0),
        )
    )
    pg_db.commit()

    response = client_with_fake_dispatcher.get("/api/v1/daily-summaries/2026-03-14")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"] is not None
    assert payload["data"]["summary_date"] == "2026-03-14"
    assert payload["data"]["summary_title"] == "2026-03-14 家庭日报"
    assert payload["data"]["detail_text"] is not None
    assert "对象小结" in payload["data"]["detail_text"]
    assert "关注事项" in payload["data"]["detail_text"]


def test_get_daily_summary_returns_401_when_unauthenticated(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.get("/api/v1/daily-summaries/2026-03-14")

    assert response.status_code == 401


def test_generate_all_daily_summaries_returns_401_when_unauthenticated(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.post("/api/v1/daily-summaries/generate-all")

    assert response.status_code == 401
