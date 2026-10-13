"""tests/integration/test_media_hls_http.py

HTTP 契约测试，覆盖 ``src/api/v1/endpoints/media.py`` 的 HLS 端点
（``playback`` / ``hls/index.m3u8``）。原 unit 测试直接调用路由函数，
本文件改为对真实 FastAPI 应用发出 HTTP 请求，断言响应码、响应体与响应头
层面的契约，并补充未签名/坏签名的鉴权 401/403 路径。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.api.v1.endpoints import media
from src.core.config import settings
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource


@pytest.fixture
def client(pg_db: Session, make_http_client) -> TestClient:
    with make_http_client([(media.router, "/api/v1/media")]) as test_client:
        yield test_client


@pytest.fixture
def anonymous_client(pg_db: Session, make_http_client) -> TestClient:
    with make_http_client([(media.router, "/api/v1/media")], authenticated=False) as test_client:
        yield test_client


@pytest.fixture
def media_workspace(tmp_path: Path) -> Iterator[Path]:
    """临时切换 ``PLAYBACK_CACHE_ROOT`` 与 ``VIDEO_ROOT_PATH`` 到 ``tmp_path``。"""
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


def _seed_two_segment_session(
    db: Session, video_root: Path, durations: tuple[int, int] = (60, 60)
) -> tuple[int, int, int]:
    """建 1 个 source + 1 个 session + 2 个 video_file，按时长推导时间轴。

    返回 ``(session_id, file_a_id, file_b_id)``。
    """
    first_duration, second_duration = durations
    session_start = datetime(2026, 3, 14, 8, 0, 0)
    first_end = session_start + timedelta(seconds=first_duration)
    session_end = first_end + timedelta(seconds=second_duration)
    source = VideoSource(
        source_name="source-1",
        camera_name="客厅",
        location_name="客厅",
        source_type="local_directory",
        enabled=True,
    )
    db.add(source)
    db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=session_start,
        session_end_time=session_end,
        total_duration_seconds=first_duration + second_duration,
        analysis_status="sealed",
    )
    db.add(session)
    db.flush()
    file_a = VideoFile(
        source_id=source.id,
        file_name="0800.mp4",
        file_path=str(video_root / "0800.mp4"),
        storage_type="local_file",
        file_format="mp4",
        start_time=session_start,
        end_time=first_end,
        duration_seconds=first_duration,
        parse_status="parsed",
    )
    file_b = VideoFile(
        source_id=source.id,
        file_name="0801.mp4",
        file_path=str(video_root / "0801.mp4"),
        storage_type="local_file",
        file_format="mp4",
        start_time=first_end,
        end_time=session_end,
        duration_seconds=second_duration,
        parse_status="parsed",
    )
    db.add_all([file_a, file_b])
    db.flush()
    db.add_all(
        [
            VideoSessionFileRel(session_id=session.id, video_file_id=file_a.id, sort_index=0),
            VideoSessionFileRel(session_id=session.id, video_file_id=file_b.id, sort_index=1),
        ]
    )
    db.commit()
    return session.id, file_a.id, file_b.id


def test_session_playback_returns_signed_playback_and_hls_urls(
    client: TestClient, pg_db: Session, media_workspace: Path
) -> None:
    (media_workspace / "0800.mp4").touch()
    (media_workspace / "0801.mp4").touch()
    session_id, _, _ = _seed_two_segment_session(pg_db, media_workspace)

    response = client.get(f"/api/v1/media/sessions/{session_id}/playback")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert data["playback_mode"] == "merged_stream"
    assert data["playback_url"].startswith(f"/media/sessions/{session_id}/stream?token=")
    assert data["hls_url"].startswith(f"/media/sessions/{session_id}/hls/index.m3u8?token=")
    assert urlsplit(data["playback_url"]).query.startswith("token=")
    assert urlsplit(data["hls_url"]).query.startswith("token=")


def test_session_playback_mode_selects_merged_stream_and_direct_file(
    client: TestClient, pg_db: Session, media_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SESSION_PLAYBACK_MODE`` chooses what ``playback_url`` points at."""
    (media_workspace / "0800.mp4").touch()
    (media_workspace / "0801.mp4").touch()
    session_id, file_a_id, _ = _seed_two_segment_session(pg_db, media_workspace)

    monkeypatch.setattr(settings, "SESSION_PLAYBACK_MODE", "merged_stream")
    merged = client.get(f"/api/v1/media/sessions/{session_id}/playback").json()["data"]
    assert merged["playback_mode"] == "merged_stream"
    assert merged["playback_url"].startswith(f"/media/sessions/{session_id}/stream?token=")

    monkeypatch.setattr(settings, "SESSION_PLAYBACK_MODE", "direct_file")
    direct = client.get(f"/api/v1/media/sessions/{session_id}/playback").json()["data"]
    assert direct["playback_mode"] == "direct_file"
    assert direct["playback_url"].startswith(f"/media/files/{file_a_id}/stream?token=")


def test_session_hls_manifest_serves_signed_segment_urls_with_no_store_cache(
    client: TestClient, pg_db: Session, media_workspace: Path
) -> None:
    (media_workspace / "0800.mp4").touch()
    (media_workspace / "0801.mp4").touch()
    session_id, file_a_id, file_b_id = _seed_two_segment_session(pg_db, media_workspace)

    playback = client.get(f"/api/v1/media/sessions/{session_id}/playback")
    hls_url = playback.json()["data"]["hls_url"]

    response = client.get(f"/api/v1{hls_url}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.text
    assert "#EXTM3U" in body
    assert "#EXT-X-TARGETDURATION:60" in body
    assert body.count("#EXTINF:60.0,") == 2
    assert f"/api/v1/media/files/{file_a_id}/stream?token=" in body
    assert f"/api/v1/media/files/{file_b_id}/stream?token=" in body


def test_session_hls_manifest_uses_real_file_durations(
    client: TestClient, pg_db: Session, media_workspace: Path
) -> None:
    """非 60 秒文件：``EXTINF`` 与 ``TARGETDURATION`` 使用真实文件时长。"""
    (media_workspace / "0800.mp4").touch()
    (media_workspace / "0801.mp4").touch()
    session_id, _, _ = _seed_two_segment_session(pg_db, media_workspace, durations=(45, 75))

    hls_url = client.get(f"/api/v1/media/sessions/{session_id}/playback").json()["data"]["hls_url"]
    body = client.get(f"/api/v1{hls_url}").text

    assert "#EXT-X-TARGETDURATION:75" in body
    assert "#EXTINF:45.0," in body
    assert "#EXTINF:75.0," in body


def test_session_playback_reports_partial_availability_when_a_segment_is_missing(
    client: TestClient, pg_db: Session, media_workspace: Path
) -> None:
    available_path = media_workspace / "available.mp4"
    available_path.touch()
    source = VideoSource(
        source_name="source-1",
        camera_name="客厅",
        location_name="客厅",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 14, 8, 0, 0),
        session_end_time=datetime(2026, 3, 14, 8, 2, 0),
        analysis_status="sealed",
    )
    pg_db.add(session)
    pg_db.flush()
    available = VideoFile(
        source_id=source.id,
        file_name="available.mp4",
        file_path=str(available_path),
        storage_type="local_file",
        start_time=datetime(2026, 3, 14, 8, 0, 0),
        end_time=datetime(2026, 3, 14, 8, 1, 0),
        parse_status="parsed",
    )
    missing = VideoFile(
        source_id=source.id,
        file_name="missing.mp4",
        file_path=str(media_workspace / "missing.mp4"),
        storage_type="local_file",
        start_time=datetime(2026, 3, 14, 8, 1, 0),
        end_time=datetime(2026, 3, 14, 8, 2, 0),
        parse_status="parsed",
    )
    pg_db.add_all([available, missing])
    pg_db.flush()
    pg_db.add_all(
        [
            VideoSessionFileRel(session_id=session.id, video_file_id=available.id, sort_index=0),
            VideoSessionFileRel(session_id=session.id, video_file_id=missing.id, sort_index=1),
        ]
    )
    pg_db.commit()
    session_id = session.id
    available_id = available.id
    missing_id = missing.id

    response = client.get(f"/api/v1/media/sessions/{session_id}/playback")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["availability"] == "partial"
    assert data["files"][0]["available"] is True
    assert data["files"][1]["available"] is False
    assert data["files"][1]["stream_url"] is None
    assert pg_db.get(VideoFile, missing_id).file_missing is True
    assert pg_db.get(VideoFile, missing_id).missing_at is not None

    manifest = client.get(f"/api/v1{data['hls_url']}")
    manifest_body = manifest.text
    assert f"/api/v1/media/files/{available_id}/stream?token=" in manifest_body
    assert f"/api/v1/media/files/{missing_id}/stream?token=" not in manifest_body


def test_session_playback_requires_authenticated_user(
    anonymous_client: TestClient, pg_db: Session, media_workspace: Path
) -> None:
    (media_workspace / "0800.mp4").touch()
    (media_workspace / "0801.mp4").touch()
    session_id, _, _ = _seed_two_segment_session(pg_db, media_workspace)

    response = anonymous_client.get(f"/api/v1/media/sessions/{session_id}/playback")

    assert response.status_code == 401


def test_session_hls_manifest_requires_signed_token(
    client: TestClient, pg_db: Session, media_workspace: Path
) -> None:
    (media_workspace / "0800.mp4").touch()
    (media_workspace / "0801.mp4").touch()
    session_id, _, _ = _seed_two_segment_session(pg_db, media_workspace)

    missing_token = client.get(f"/api/v1/media/sessions/{session_id}/hls/index.m3u8")
    assert missing_token.status_code == 401

    playback = client.get(f"/api/v1/media/sessions/{session_id}/playback")
    good_token = playback.json()["data"]["hls_url"].split("token=", 1)[1]
    tampered = client.get(f"/api/v1/media/sessions/{session_id}/hls/index.m3u8?token={good_token}x")
    assert tampered.status_code == 403
