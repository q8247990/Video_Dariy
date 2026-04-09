import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from src.api.deps import DB, Locale
from src.core.i18n import t
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.schemas.response import BaseResponse
from src.services.session_playback import get_or_create_session_hls_manifest
from src.services.session_video import ensure_merged_video

router = APIRouter()

# Note: Media endpoints are typically separated from API_V1_STR in config,
# but we map it here for simplicity. We may need separate JWT verification for media
# if we want to stream directly to <video src="...">, often done via query param token.


def send_bytes_range_requests(file_obj, start: int, end: int, chunk_size: int = 1024 * 1024):
    """Send a file in chunks for Range requests."""
    with file_obj as f:
        f.seek(start)
        while (pos := f.tell()) <= end:
            read_size = min(chunk_size, end + 1 - pos)
            yield f.read(read_size)


@router.get("/files/{file_id}/stream")
def stream_video(file_id: int, db: DB, locale: Locale, request: Request):
    video_file = db.query(VideoFile).filter(VideoFile.id == file_id).first()
    if not video_file:
        raise HTTPException(status_code=404, detail=t("media.video_file_not_found", locale))

    path = video_file.file_path
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=t("media.physical_file_not_found", locale))

    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range")

    if range_header:
        byte1, byte2 = 0, None
        match = range_header.replace("bytes=", "").split("-")
        byte1 = int(match[0])
        if match[1]:
            byte2 = int(match[1])
        else:
            byte2 = file_size - 1

        length = byte2 - byte1 + 1

        headers = {
            "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": "video/mp4",
        }

        return StreamingResponse(
            send_bytes_range_requests(open(path, "rb"), byte1, byte2),
            status_code=206,
            headers=headers,
        )
    else:
        return FileResponse(path, media_type="video/mp4")


@router.get("/sessions/{session_id}/playback", response_model=BaseResponse[dict])
def get_session_playback(session_id: int, db: DB, locale: Locale):
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if not session:
        return BaseResponse(code=4002, message=t("session.not_found", locale))

    rels = (
        db.query(VideoSessionFileRel)
        .filter(VideoSessionFileRel.session_id == session_id)
        .order_by(VideoSessionFileRel.sort_index)
        .all()
    )

    files_data = []
    for rel in rels:
        vf = db.query(VideoFile).filter(VideoFile.id == rel.video_file_id).first()
        if vf:
            files_data.append(
                {
                    "file_id": vf.id,
                    "file_name": vf.file_name,
                    "stream_url": f"/media/files/{vf.id}/stream",
                    "sort_index": rel.sort_index,
                }
            )

    return BaseResponse(
        data={
            "session_id": session.id,
            "session_start_time": session.session_start_time,
            "session_end_time": session.session_end_time,
            "playback_url": f"/media/sessions/{session.id}/stream",
            "hls_url": f"/media/sessions/{session.id}/hls/index.m3u8",
            "files": files_data,
        }
    )


@router.get("/sessions/{session_id}/hls/index.m3u8")
def stream_session_hls_manifest(session_id: int, db: DB, locale: Locale):
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail=t("session.not_found", locale))

    try:
        manifest_info = get_or_create_session_hls_manifest(db, session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if not manifest_info.manifest_path.exists():
        raise HTTPException(status_code=404, detail=t("media.hls_manifest_not_found", locale))

    try:
        content = manifest_info.manifest_path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=t("media.hls_read_failed", locale, error=e)
        ) from e

    return Response(content=content, media_type="application/vnd.apple.mpegurl")


@router.get("/sessions/{session_id}/stream")
def stream_session_merged_video(session_id: int, db: DB, locale: Locale, request: Request):
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail=t("session.not_found", locale))

    try:
        path = ensure_merged_video(db, session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=t("media.merged_video_not_found", locale))

    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range")

    if range_header:
        byte1, byte2 = 0, None
        match = range_header.replace("bytes=", "").split("-")
        byte1 = int(match[0])
        if match[1]:
            byte2 = int(match[1])
        else:
            byte2 = file_size - 1

        length = byte2 - byte1 + 1
        headers = {
            "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": "video/mp4",
        }

        return StreamingResponse(
            send_bytes_range_requests(open(path, "rb"), byte1, byte2),
            status_code=206,
            headers=headers,
        )

    return FileResponse(path, media_type="video/mp4")
