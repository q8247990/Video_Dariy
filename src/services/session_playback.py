import json
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from src.core.config import settings
from src.services.session_video import get_session_video_files

_SESSION_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass
class HlsManifestInfo:
    manifest_path: Path
    manifest_url: str


def get_or_create_session_hls_manifest(db: Session, session_id: int) -> HlsManifestInfo:
    lock = _get_session_lock(session_id)
    with lock:
        manifest_path = _get_session_manifest_path(session_id)
        video_files = get_session_video_files(db, session_id)
        _write_index_manifest(
            session_id=session_id, manifest_path=manifest_path, file_ids=[v.id for v in video_files]
        )
        return HlsManifestInfo(
            manifest_path=manifest_path,
            manifest_url=f"/media/sessions/{session_id}/hls/index.m3u8",
        )


def _get_session_lock(session_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session_id] = lock
    return lock


def _get_hls_cache_root() -> Path:
    root = Path(settings.PLAYBACK_CACHE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _get_session_manifest_path(session_id: int) -> Path:
    session_dir = _get_hls_cache_root() / f"session_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "index.m3u8"


def _write_index_manifest(session_id: int, manifest_path: Path, file_ids: list[int]) -> None:
    if not file_ids:
        raise ValueError(f"Session {session_id} has no playable video files")

    target_duration = 60
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]

    for file_id in file_ids:
        lines.append("#EXTINF:60.0,")
        lines.append(f"{settings.API_V1_STR}/media/files/{file_id}/stream")

    lines.append("#EXT-X-ENDLIST")
    payload = "\n".join(lines) + "\n"

    tmp_manifest = manifest_path.with_suffix(".m3u8.tmp")
    tmp_manifest.write_text(payload, encoding="utf-8")
    tmp_manifest.replace(manifest_path)

    meta_path = manifest_path.with_name("meta.json")
    meta_payload = {"session_id": session_id, "file_ids": file_ids}
    meta_path.write_text(json.dumps(meta_payload, ensure_ascii=True), encoding="utf-8")
