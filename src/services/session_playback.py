import fcntl
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy.orm import Session

from src.core.config import settings
from src.services.media_signing import MediaCapability, MediaSigningService
from src.services.session_video import get_session_video_files, resolve_file_duration_seconds
from src.services.system_config_registry import MEDIA_SEGMENT_TTL_SECONDS, get_config


@dataclass
class HlsManifestInfo:
    manifest_path: Path
    manifest_url: str


@contextmanager
def _manifest_write_lock(manifest_path: Path) -> Iterator[None]:
    lock_path = manifest_path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def get_or_create_session_hls_manifest(db: Session, session_id: int) -> HlsManifestInfo:
    manifest_path = _get_session_manifest_path(session_id)
    segment_ttl_seconds = int(get_config(db, MEDIA_SEGMENT_TTL_SECONDS))
    with _manifest_write_lock(manifest_path):
        video_files = get_session_video_files(db, session_id)
        available_files = [
            (video.id, resolve_file_duration_seconds(video))
            for video in video_files
            if not video.file_missing
        ]
        _write_index_manifest(
            session_id, manifest_path, available_files, segment_ttl_seconds=segment_ttl_seconds
        )
    return HlsManifestInfo(
        manifest_path=manifest_path,
        manifest_url=f"/media/sessions/{session_id}/hls/index.m3u8",
    )


def _get_hls_cache_root() -> Path:
    root = Path(settings.PLAYBACK_CACHE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _get_session_manifest_path(session_id: int) -> Path:
    session_dir = _get_hls_cache_root() / f"session_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "index.m3u8"


def _write_index_manifest(
    session_id: int,
    manifest_path: Path,
    files: list[tuple[int, int]],
    *,
    segment_ttl_seconds: int,
) -> None:
    if not files:
        raise ValueError(f"Session {session_id} has no available video files")

    target_duration = max(1, max(duration for _, duration in files))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    signing_service = MediaSigningService.from_settings()
    expires_at = int(time.time()) + segment_ttl_seconds
    for file_id, duration in files:
        lines.append(f"#EXTINF:{duration}.0,")
        capability = MediaCapability(
            resource_kind="file",
            resource_id=file_id,
            method="GET",
            expires_at=expires_at,
            session_parent_id=session_id,
        )
        lines.append(
            signing_service.signed_url(
                f"{settings.API_V1_STR}/media/files/{file_id}/stream", capability
            )
        )
    lines.append("#EXT-X-ENDLIST")
    tmp_manifest = manifest_path.with_suffix(".m3u8.tmp")
    tmp_manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp_manifest.replace(manifest_path)
