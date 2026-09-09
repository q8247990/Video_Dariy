from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import src.api.deps as api_deps
from src.api.v1.endpoints import media
from src.core.config import settings
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.media_signing import MediaCapability, MediaSigningService


@pytest.fixture
def db_session(pg_db: Session) -> Session:
    """Single session against the project-wide ``pg_db`` fixture (PostgreSQL)."""
    return pg_db


@pytest.fixture
def media_workspace(tmp_path: Path) -> Iterator[Path]:
    """临时切换 ``PLAYBACK_CACHE_ROOT`` 与 ``VIDEO_ROOT_PATH`` 到隔离目录。"""
    with TemporaryDirectory() as cache_root:
        old_cache_root = settings.PLAYBACK_CACHE_ROOT
        old_video_root = settings.VIDEO_ROOT_PATH
        settings.PLAYBACK_CACHE_ROOT = cache_root
        settings.VIDEO_ROOT_PATH = str(tmp_path)
        try:
            yield tmp_path
        finally:
            settings.PLAYBACK_CACHE_ROOT = old_cache_root
            settings.VIDEO_ROOT_PATH = old_video_root


@pytest.fixture
def client(pg_db: Session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(media.router, prefix="/api/v1/media")

    def _override_get_db() -> Iterator[Session]:
        yield pg_db

    def _override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(id=1, username="admin")

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    app.dependency_overrides[api_deps.get_current_user] = _override_get_current_user
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _seed_playable_session(db: Session, video_path: Path) -> tuple[int, int]:
    source = VideoSource(
        source_name="signed-media",
        camera_name="cam",
        location_name="loc",
        source_type="local_directory",
    )
    db.add(source)
    db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 14, 8, 0, 0),
        session_end_time=datetime(2026, 3, 14, 8, 1, 0),
        total_duration_seconds=60,
        analysis_status="pending",
    )
    db.add(session)
    db.flush()
    video_file = VideoFile(
        source_id=source.id,
        file_name="0800.mp4",
        file_path=str(video_path),
        storage_type="local_file",
        file_format="mp4",
        start_time=session.session_start_time,
        end_time=session.session_end_time,
        duration_seconds=60,
        parse_status="parsed",
    )
    db.add(video_file)
    db.flush()
    db.add(VideoSessionFileRel(session_id=session.id, video_file_id=video_file.id, sort_index=0))
    db.commit()
    return session.id, video_file.id


def test_media_stream_serves_in_bounds_range_and_rejects_cross_resource(
    client: TestClient, db_session: Session, media_workspace: Path
) -> None:
    """in-bounds Range 返回 206 片段；签名能力越界到其他 file 时拒绝。

    未签名 401、签名 URL 签发、manifest no-store 等契约由
    ``tests/integration/test_media_hls_http.py`` 覆盖，此处不重复。
    """
    video_path = media_workspace / "clip.mp4"
    video_path.write_bytes(b"abcdefghij")
    session_id, file_id = _seed_playable_session(db_session, video_path)

    playback = client.get(f"/api/v1/media/sessions/{session_id}/playback")
    file_url = playback.json()["data"]["files"][0]["stream_url"]

    ranged = client.get(f"/api/v1{file_url}", headers={"Range": "bytes=3-6"})
    cross_resource = client.get(f"/api/v1{file_url.replace(f'/{file_id}/', f'/{file_id + 1}/')}")

    assert ranged.status_code == 206
    assert ranged.content == b"defg"
    assert cross_resource.status_code == 403


def test_signed_media_rejects_tampered_and_cross_session_segment_tokens(
    client: TestClient, db_session: Session, media_workspace: Path
) -> None:
    video_path = media_workspace / "clip.mp4"
    video_path.write_bytes(b"abcdefghij")
    session_id, file_id = _seed_playable_session(db_session, video_path)
    capability = MediaCapability(
        resource_kind="file",
        resource_id=file_id,
        method="GET",
        expires_at=2_000_000_000,
        session_parent_id=session_id + 1,
    )
    token = MediaSigningService.from_settings().issue(capability)

    cross_session = client.get(f"/api/v1/media/files/{file_id}/stream?token={token}")
    tampered = client.get(f"/api/v1/media/files/{file_id}/stream?token={token}x")

    assert cross_session.status_code == 403
    assert tampered.status_code == 403


@pytest.mark.parametrize(
    ("range_header", "expected_status", "expected_body"),
    [
        ("bytes=5-1", 416, None),
        ("bytes=100-200", 416, None),
        ("bytes=abc", 416, None),
        ("bytes=-", 416, None),
        ("bytes=-0", 416, None),
        ("bytes=-3", 206, b"hij"),
        ("bytes=-100", 206, b"abcdefghij"),
    ],
)
def test_range_headers_have_documented_http_contract(
    client: TestClient,
    db_session: Session,
    media_workspace: Path,
    range_header: str,
    expected_status: int,
    expected_body: bytes | None,
) -> None:
    """Range 头在文件流端点的 HTTP 契约（RFC 7233）。

    格式合法但语义越界的 Range（``bytes=5-1`` start > end、``bytes=100-200``
    超出文件长度）与完全无法解析的 Range（``bytes=abc``、``bytes=-``）均返回
    416 + ``Content-Range: bytes */<size>``，不得出现 500。

    ``bytes=-N`` 是合法的 suffix range（文件末尾 N 字节）：``bytes=-3`` 返回
    末尾 3 字节；``bytes=-100`` 请求超过文件长度，返回整个 10 字节文件。
    """
    video_path = media_workspace / "clip.mp4"
    video_path.write_bytes(b"abcdefghij")
    session_id, file_id = _seed_playable_session(db_session, video_path)

    playback = client.get(f"/api/v1/media/sessions/{session_id}/playback")
    file_url = playback.json()["data"]["files"][0]["stream_url"]

    response = client.get(f"/api/v1{file_url}", headers={"Range": range_header})

    assert response.status_code == expected_status
    if expected_status == 206:
        assert response.content == expected_body
    else:
        assert response.headers["content-range"] == "bytes */10"
