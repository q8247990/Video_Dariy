"""Real-HTTP contract tests for the video_sources endpoints.

The previous :mod:`tests.unit.test_video_source_endpoint_guards` invoked
the route functions directly with hand-built ``SimpleNamespace`` users
and skipped FastAPI's request/response boundary. Those tests asserted
implementation-level return values instead of the public HTTP contract
the rest of the system actually depends on.

This module ports every business-rule case from the old unit file to a
real :class:`fastapi.testclient.TestClient` and adds the wire-level
assertions the project standard requires:

* HTTP status code (including the ``ResponseStatusMiddleware`` mapping
  of business codes 4000/4002/4004 to 400/404/409)
* ``code`` field inside the JSON body
* Observable database state after the call
* 401 for unauthenticated access and 422 for invalid Pydantic payloads

The endpoints not covered here (GET list, POST create, pause/resume,
``/test``, ``/validate-path``) are exercised by
:mod:`tests.integration.test_onboarding_http`; this module deliberately
mirrors only the four cases from the original unit test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.api.error_status import ResponseStatusMiddleware
from src.api.v1.endpoints import video_sources
from src.models.event_record import EventRecord
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import TaskStatus, TaskType


@pytest.fixture
def client(pg_db: Session, make_http_client) -> Iterator[TestClient]:
    """Authenticated TestClient with business-code -> HTTP status mapping."""

    with make_http_client(
        [(video_sources.router, "/api/v1/video-sources")],
        middleware=[ResponseStatusMiddleware],
    ) as test_client:
        yield test_client


@pytest.fixture
def anonymous_client(pg_db: Session, make_http_client) -> Iterator[TestClient]:
    """Anonymous TestClient (no ``get_current_user`` override) for 401 assertions.

    No middleware is installed here: 401 is produced by FastAPI's own
    OAuth2 dependency, not by ``ResponseStatusMiddleware``.
    """

    with make_http_client(
        [(video_sources.router, "/api/v1/video-sources")],
        authenticated=False,
    ) as test_client:
        yield test_client


def _build_source(
    *,
    name: str,
    camera: str,
    location: str,
    config_json: dict | None = None,
    enabled: bool = True,
    last_validate_status: str | None = None,
    last_validate_message: str | None = None,
    last_validate_at: datetime | None = None,
) -> VideoSource:
    return VideoSource(
        source_name=name,
        camera_name=camera,
        location_name=location,
        source_type="local_directory",
        config_json=config_json if config_json is not None else {"root_path": "/tmp/a"},
        enabled=enabled,
        last_validate_status=last_validate_status,
        last_validate_message=last_validate_message,
        last_validate_at=last_validate_at,
    )


# ---------------------------------------------------------------------------
# PUT /api/v1/video-sources/{id}
# ---------------------------------------------------------------------------


def test_update_video_source_resets_validation_when_config_changed(
    client: TestClient, pg_db: Session
) -> None:
    """Ported from unit test of the same name.

    When ``config_json`` is updated, the cached last-validate snapshot
    must be cleared on the persisted row — not just on the response.
    """

    source = _build_source(
        name="客厅",
        camera="cam1",
        location="客厅",
        last_validate_status="success",
        last_validate_message="ok",
        last_validate_at=datetime(2026, 3, 10, 10, 0, 0),
    )
    pg_db.add(source)
    pg_db.commit()

    response = client.put(
        f"/api/v1/video-sources/{source.id}",
        json={"config_json": {"root_path": "/tmp/b"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["last_validate_status"] is None
    assert body["data"]["last_validate_message"] is None
    assert body["data"]["last_validate_at"] is None

    persisted = pg_db.get(VideoSource, source.id)
    assert persisted is not None
    assert persisted.last_validate_status is None
    assert persisted.last_validate_message is None
    assert persisted.last_validate_at is None
    assert persisted.config_json == {"root_path": "/tmp/b"}


def test_update_video_source_returns_404_when_source_not_found(
    client: TestClient,
) -> None:
    response = client.put(
        "/api/v1/video-sources/9999",
        json={"config_json": {"root_path": "/tmp/b"}},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 4002


def test_update_video_source_returns_422_for_invalid_payload(
    client: TestClient, pg_db: Session
) -> None:
    """A wrong-typed field on ``VideoSourceUpdate`` fails Pydantic at the
    boundary before the route runs; FastAPI emits its own 422 response
    (which intentionally does NOT go through the business-code mapper)."""

    source = _build_source(name="客厅", camera="cam1", location="客厅")
    pg_db.add(source)
    pg_db.commit()

    response = client.put(
        f"/api/v1/video-sources/{source.id}",
        json={"enabled": "not-a-bool"},
    )

    assert response.status_code == 422

    persisted = pg_db.get(VideoSource, source.id)
    assert persisted is not None
    assert persisted.enabled is True


def test_update_video_source_returns_401_when_unauthenticated(
    anonymous_client: TestClient, pg_db: Session
) -> None:
    source = _build_source(name="客厅", camera="cam1", location="客厅")
    pg_db.add(source)
    pg_db.commit()

    response = anonymous_client.put(
        f"/api/v1/video-sources/{source.id}",
        json={"config_json": {"root_path": "/tmp/b"}},
    )

    assert response.status_code == 401

    persisted = pg_db.get(VideoSource, source.id)
    assert persisted is not None
    assert persisted.config_json == {"root_path": "/tmp/a"}


# ---------------------------------------------------------------------------
# DELETE /api/v1/video-sources/{id}
# ---------------------------------------------------------------------------


def test_delete_video_source_blocks_running_task(client: TestClient, pg_db: Session) -> None:
    """Ported from unit test of the same name.

    A source with a running ``SESSION_BUILD`` task must NOT be deleted
    and the response carries business code 4004 (-> HTTP 409).
    """

    source = _build_source(name="门口", camera="cam2", location="门口")
    pg_db.add(source)
    pg_db.flush()
    pg_db.add(
        TaskLog(
            task_type=TaskType.SESSION_BUILD,
            task_target_id=source.id,
            status=TaskStatus.RUNNING,
        )
    )
    pg_db.commit()

    response = client.delete(f"/api/v1/video-sources/{source.id}")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004
    assert "running task" in body["message"]

    assert pg_db.get(VideoSource, source.id) is not None


def test_delete_video_source_restricts_retained_event_history(
    client: TestClient, pg_db: Session
) -> None:
    """Ported from unit test of the same name.

    A source with a retained ``EventRecord`` is protected from deletion
    so historical event rows keep their source_id reference intact.
    """

    source = _build_source(name="history", camera="cam-history", location="test")
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 10, 10, 0, 0),
        session_end_time=datetime(2026, 3, 10, 10, 1, 0),
        analysis_status="success",
    )
    pg_db.add(session)
    pg_db.flush()
    pg_db.add(
        EventRecord(
            source_id=source.id,
            session_id=session.id,
            event_start_time=datetime(2026, 3, 10, 10, 0, 0),
            description="retained history",
        )
    )
    pg_db.commit()

    response = client.delete(f"/api/v1/video-sources/{source.id}")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 4004

    assert pg_db.get(VideoSource, source.id) is not None
    assert pg_db.get(VideoSession, session.id) is not None


def test_delete_video_source_succeeds_when_no_history_and_no_task(
    client: TestClient, pg_db: Session
) -> None:
    """Happy path complement of the two guard cases above.

    Without running tasks or retained events, DELETE must remove the
    row and report business code 0 (HTTP 200).
    """

    source = _build_source(name="clean", camera="cam-clean", location="lobby")
    pg_db.add(source)
    pg_db.commit()

    response = client.delete(f"/api/v1/video-sources/{source.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0

    assert pg_db.get(VideoSource, source.id) is None


def test_delete_video_source_returns_404_when_not_found(client: TestClient) -> None:
    response = client.delete("/api/v1/video-sources/9999")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 4002


def test_delete_video_source_returns_401_when_unauthenticated(
    anonymous_client: TestClient, pg_db: Session
) -> None:
    source = _build_source(name="客厅", camera="cam1", location="客厅")
    pg_db.add(source)
    pg_db.commit()

    response = anonymous_client.delete(f"/api/v1/video-sources/{source.id}")

    assert response.status_code == 401
    assert pg_db.get(VideoSource, source.id) is not None


# ---------------------------------------------------------------------------
# GET /api/v1/video-sources/status/batch
# ---------------------------------------------------------------------------


def test_video_source_status_batch_returns_multiple_sources(
    client: TestClient, pg_db: Session
) -> None:
    """Ported from unit test of the same name."""

    s1 = _build_source(name="A", camera="camA", location="A", config_json={"root_path": "/tmp"})
    s2 = _build_source(name="B", camera="camB", location="B", config_json={"root_path": "/tmp"})
    pg_db.add_all([s1, s2])
    pg_db.commit()

    response = client.get(
        "/api/v1/video-sources/status/batch",
        params={"source_ids": f"{s1.id},{s2.id}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert len(body["data"]) == 2
    assert {item["source_id"] for item in body["data"]} == {s1.id, s2.id}


def test_video_source_status_batch_returns_401_when_unauthenticated(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.get(
        "/api/v1/video-sources/status/batch",
        params={"source_ids": "1"},
    )

    assert response.status_code == 401
