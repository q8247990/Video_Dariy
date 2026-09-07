import os
import time
from collections.abc import Iterator
from datetime import datetime
from typing import IO

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

from src.api.deps import DB, CurrentUser, Locale
from src.core.i18n import t
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.schemas.response import BaseResponse
from src.services.media_signing import (
    MANIFEST_TTL_SECONDS,
    SEGMENT_TTL_SECONDS,
    MediaCapability,
    MediaCapabilityError,
    MediaResourceKind,
    MediaSigningService,
)
from src.services.session_playback import get_or_create_session_hls_manifest
from src.services.session_video import (
    SessionVideoUnavailableError,
    ensure_merged_video,
    is_video_file_available,
    mark_missing_video_file,
    resolve_video_file_path,
)

router = APIRouter()


def _clamp_range_end(byte2: int, file_size: int) -> int:
    """Clamp the inclusive end offset to the last valid byte index."""
    return min(byte2, file_size - 1)


def _parse_byte_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """Parse an RFC 7233 ``bytes`` Range header into inclusive offsets.

    Returns ``(start, end)`` for a satisfiable range, or ``None`` when the
    request carries no Range header. Raises ``ValueError`` when the header is
    malformed or the requested range cannot be satisfied by the file; the
    stream endpoints translate that into HTTP 416.
    """
    if not range_header:
        return None
    if not range_header.startswith("bytes="):
        raise ValueError("unsupported Range unit")
    if file_size == 0:
        raise ValueError("range not satisfiable")
    start_text, sep, end_text = range_header.removeprefix("bytes=").partition("-")
    if sep == "":
        raise ValueError("malformed Range header")

    if start_text == "":
        # Suffix range spec (RFC 7233 §3.1): the final N bytes of the file.
        try:
            suffix = int(end_text)
        except ValueError:
            raise ValueError("malformed Range header") from None
        if suffix <= 0:
            raise ValueError("range not satisfiable")
        return max(0, file_size - suffix), _clamp_range_end(file_size - 1, file_size)

    try:
        start = int(start_text)
    except ValueError:
        raise ValueError("malformed Range header") from None
    end = file_size - 1 if end_text == "" else int(end_text)
    if start >= file_size or end < start:
        raise ValueError("range not satisfiable")
    return start, _clamp_range_end(end, file_size)


def _resolve_byte_range(request: Request, locale: Locale, file_size: int) -> tuple[int, int] | None:
    """Parse the request Range header, answering 416 for invalid or unsatisfiable ranges."""
    try:
        return _parse_byte_range(request.headers.get("Range"), file_size)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=t("media.range_not_satisfiable", locale),
            headers={"Content-Range": f"bytes */{file_size}"},
        ) from None


def send_bytes_range_requests(
    file_obj: IO[bytes], start: int, end: int, chunk_size: int = 1024 * 1024
) -> Iterator[bytes]:
    """Stream a byte range from ``file_obj``. Stops cleanly at EOF or end offset."""
    with file_obj as f:
        f.seek(start)
        while True:
            pos = f.tell()
            if pos > end:
                break
            chunk = f.read(min(chunk_size, end - pos + 1))
            if not chunk:
                break
            yield chunk


def _capability_url(
    path: str,
    resource_kind: MediaResourceKind,
    resource_id: int,
    ttl_seconds: int,
    session_parent_id: int | None = None,
) -> str:
    capability = MediaCapability(
        resource_kind=resource_kind,
        resource_id=resource_id,
        method="GET",
        expires_at=int(time.time()) + ttl_seconds,
        session_parent_id=session_parent_id,
    )
    return MediaSigningService.from_settings().signed_url(path, capability)


def _verify_media_capability(
    token: str | None,
    resource_kind: MediaResourceKind,
    resource_id: int,
    session_parent_id: int | None = None,
) -> MediaCapability:
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing media capability"
        )

    capability = MediaCapability(
        resource_kind=resource_kind,
        resource_id=resource_id,
        method="GET",
        expires_at=0,
        session_parent_id=session_parent_id,
    )
    try:
        return MediaSigningService.from_settings().verify(token, capability)
    except MediaCapabilityError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid media capability"
        ) from error


def _verify_file_session_scope(db: DB, file_id: int, capability: MediaCapability) -> None:
    if capability.session_parent_id is None:
        return
    relation = (
        db.query(VideoSessionFileRel)
        .filter(
            VideoSessionFileRel.session_id == capability.session_parent_id,
            VideoSessionFileRel.video_file_id == file_id,
        )
        .first()
    )
    if relation is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid media capability"
        )


def _no_store_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store"}


@router.get("/files/{file_id}/stream", response_model=None)
def stream_video(
    file_id: int, db: DB, locale: Locale, request: Request, token: str | None = Query(default=None)
) -> FileResponse | StreamingResponse:
    capability = _verify_media_capability(token, "file", file_id)
    _verify_file_session_scope(db, file_id, capability)
    video_file = db.query(VideoFile).filter(VideoFile.id == file_id).first()
    if not video_file:
        raise HTTPException(status_code=404, detail=t("media.video_file_not_found", locale))

    path = resolve_video_file_path(video_file)
    if path is None or not path.is_file():
        mark_missing_video_file(video_file)
        db.commit()
        raise HTTPException(status_code=404, detail=t("media.physical_file_not_found", locale))

    file_size = path.stat().st_size
    byte_range = _resolve_byte_range(request, locale, file_size)

    if byte_range:
        byte1, byte2 = byte_range

        length = byte2 - byte1 + 1

        headers = {
            "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": "video/mp4",
            **_no_store_headers(),
        }

        return StreamingResponse(
            send_bytes_range_requests(path.open("rb"), byte1, byte2),
            status_code=206,
            headers=headers,
        )
    else:
        return FileResponse(str(path), media_type="video/mp4", headers=_no_store_headers())


@router.get("/sessions/{session_id}/playback", response_model=BaseResponse[dict])
def get_session_playback(
    session_id: int, db: DB, locale: Locale, current_user: CurrentUser
) -> BaseResponse[dict]:
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if not session:
        return BaseResponse(code=4002, message=t("session.not_found", locale))

    rels = (
        db.query(VideoSessionFileRel)
        .filter(VideoSessionFileRel.session_id == session_id)
        .order_by(VideoSessionFileRel.sort_index)
        .all()
    )

    video_file_ids = [rel.video_file_id for rel in rels]
    video_files = (
        db.query(VideoFile).filter(VideoFile.id.in_(video_file_ids)).all() if video_file_ids else []
    )
    video_files_by_id = {video_file.id: video_file for video_file in video_files}

    files_data = []
    for rel in rels:
        vf = video_files_by_id.get(rel.video_file_id)
        if vf is None:
            files_data.append(
                {
                    "file_id": rel.video_file_id,
                    "file_name": None,
                    "stream_url": None,
                    "sort_index": rel.sort_index,
                    "available": False,
                    "unavailable_reason": "video_file_record_missing",
                }
            )
            continue
        available = is_video_file_available(vf)
        if not available:
            mark_missing_video_file(vf)
        missing_at: datetime | None = vf.missing_at
        files_data.append(
            {
                "file_id": vf.id,
                "file_name": vf.file_name,
                "stream_url": (
                    _capability_url(
                        f"/media/files/{vf.id}/stream",
                        "file",
                        vf.id,
                        SEGMENT_TTL_SECONDS,
                        session.id,
                    )
                    if available
                    else None
                ),
                "sort_index": rel.sort_index,
                "available": available,
                "unavailable_reason": None if available else "physical_file_unavailable",
                "missing_at": missing_at.isoformat() if missing_at else None,
            }
        )

    available_count = sum(1 for file_data in files_data if file_data["available"])
    if available_count != len(files_data):
        db.commit()
    availability = "available"
    if available_count == 0:
        availability = "unavailable"
    elif available_count != len(files_data):
        availability = "partial"

    return BaseResponse(
        data={
            "session_id": session.id,
            "session_start_time": session.session_start_time,
            "session_end_time": session.session_end_time,
            "playback_url": _capability_url(
                f"/media/sessions/{session.id}/stream",
                "session_stream",
                session.id,
                SEGMENT_TTL_SECONDS,
            ),
            "hls_url": _capability_url(
                f"/media/sessions/{session.id}/hls/index.m3u8",
                "session_hls",
                session.id,
                MANIFEST_TTL_SECONDS,
            ),
            "files": files_data,
            "availability": availability,
        }
    )


@router.get("/sessions/{session_id}/hls/index.m3u8", response_model=None)
def stream_session_hls_manifest(
    session_id: int, db: DB, locale: Locale, token: str | None = Query(default=None)
) -> Response:
    _verify_media_capability(token, "session_hls", session_id)
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail=t("session.not_found", locale))

    try:
        manifest_info = get_or_create_session_hls_manifest(db, session_id)
    except SessionVideoUnavailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if not manifest_info.manifest_path.exists():
        raise HTTPException(status_code=404, detail=t("media.hls_manifest_not_found", locale))

    try:
        content = manifest_info.manifest_path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=t("media.hls_read_failed", locale, error=e)
        ) from e

    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers=_no_store_headers(),
    )


@router.get("/sessions/{session_id}/stream", response_model=None)
def stream_session_merged_video(
    session_id: int,
    db: DB,
    locale: Locale,
    request: Request,
    token: str | None = Query(default=None),
) -> FileResponse | StreamingResponse:
    _verify_media_capability(token, "session_stream", session_id)
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail=t("session.not_found", locale))

    try:
        path = ensure_merged_video(db, session_id)
    except SessionVideoUnavailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=t("media.merged_video_not_found", locale))

    file_size = os.path.getsize(path)
    byte_range = _resolve_byte_range(request, locale, file_size)

    if byte_range:
        byte1, byte2 = byte_range

        length = byte2 - byte1 + 1
        headers = {
            "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": "video/mp4",
            **_no_store_headers(),
        }

        return StreamingResponse(
            send_bytes_range_requests(open(path, "rb"), byte1, byte2),
            status_code=206,
            headers=headers,
        )

    return FileResponse(path, media_type="video/mp4", headers=_no_store_headers())
