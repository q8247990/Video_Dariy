from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.api.deps as api_deps
from src.api.v1.endpoints import media
from src.core.config import settings
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.services.media_signing import MediaCapability, MediaSigningService


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    VideoSession.__table__.create(bind=engine)
    VideoFile.__table__.create(bind=engine)
    VideoSessionFileRel.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = local_session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(media.router, prefix="/api/v1/media")

    def _override_get_db():
        yield db

    def _override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(id=1, username="admin")

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    app.dependency_overrides[api_deps.get_current_user] = _override_get_current_user
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _seed_playable_session(db: Session, video_path: Path) -> tuple[int, int]:
    session = VideoSession(
        source_id=1,
        session_start_time=datetime(2026, 3, 14, 8, 0, 0),
        session_end_time=datetime(2026, 3, 14, 8, 1, 0),
        total_duration_seconds=60,
        analysis_status="pending",
    )
    db.add(session)
    db.flush()
    video_file = VideoFile(
        source_id=1,
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


def test_media_routes_require_scoped_signed_capabilities_and_preserve_ranges(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    old_video_root = settings.VIDEO_ROOT_PATH
    settings.VIDEO_ROOT_PATH = str(tmp_path)
    video_path = tmp_path / "clip.mp4"
    try:
        video_path.write_bytes(b"abcdefghij")
        session_id, file_id = _seed_playable_session(db, video_path)

        unsigned = client.get(f"/api/v1/media/files/{file_id}/stream")
        playback = client.get(f"/api/v1/media/sessions/{session_id}/playback")

        assert unsigned.status_code == 401
        assert playback.status_code == 200
        playback_data = playback.json()["data"]
        file_url = playback_data["files"][0]["stream_url"]
        manifest_url = playback_data["hls_url"]
        ranged = client.get(f"/api/v1{file_url}", headers={"Range": "bytes=3-6"})
        manifest = client.get(f"/api/v1{manifest_url}")

        assert ranged.status_code == 206
        assert ranged.content == b"defg"
        assert ranged.headers["cache-control"] == "no-store"
        assert manifest.status_code == 200
        assert manifest.headers["cache-control"] == "no-store"
        segment_url = next(
            line for line in manifest.text.splitlines() if line.startswith("/api/v1/media/files/")
        )
        segment = client.get(segment_url)
        cross_resource = client.get(
            f"/api/v1{file_url.replace(f'/{file_id}/', f'/{file_id + 1}/')}"
        )

        assert segment.status_code == 200
        assert segment.content == b"abcdefghij"
        assert cross_resource.status_code == 403
    finally:
        settings.VIDEO_ROOT_PATH = old_video_root


def test_hls_manifest_url_contains_a_signed_query_capability(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"abcdefghij")
    session_id, _ = _seed_playable_session(db, video_path)

    playback = client.get(f"/api/v1/media/sessions/{session_id}/playback")
    hls_url = playback.json()["data"]["hls_url"]
    parsed = urlsplit(hls_url)

    assert parsed.path == f"/media/sessions/{session_id}/hls/index.m3u8"
    assert parsed.query.startswith("token=")


def test_signed_media_rejects_tampered_and_cross_session_segment_tokens(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"abcdefghij")
    session_id, file_id = _seed_playable_session(db, video_path)
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
    db: Session,
    tmp_path: Path,
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
    old_video_root = settings.VIDEO_ROOT_PATH
    settings.VIDEO_ROOT_PATH = str(tmp_path)
    video_path = tmp_path / "clip.mp4"
    try:
        video_path.write_bytes(b"abcdefghij")
        _, file_id = _seed_playable_session(db, video_path)

        playback = client.get("/api/v1/media/sessions/1/playback")
        file_url = playback.json()["data"]["files"][0]["stream_url"]

        response = client.get(f"/api/v1{file_url}", headers={"Range": range_header})

        assert response.status_code == expected_status
        if expected_status == 206:
            assert response.content == expected_body
        else:
            assert response.headers["content-range"] == "bytes */10"
    finally:
        settings.VIDEO_ROOT_PATH = old_video_root
