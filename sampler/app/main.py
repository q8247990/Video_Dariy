"""Frame sampler service application (scaffold todo 15, /sample todo 19).

Out-of-process service that samples frames from surveillance recordings for
the main pipeline. It exposes:

- ``GET /healthz`` — liveness and version (always 200).
- ``POST /sample`` — decode a whitelisted video file at a fixed probe rate,
  select a bounded set of frames, and return them as base64 JPEGs with the
  elapsed extraction time.

Candidate windows come from ffmpeg scene detection and drive the ``event``
selection mode internally; they are not part of the HTTP response. The ONNX
person detector was removed until a consumer path exists, so ``/sample`` is
not person-aware.
"""

from __future__ import annotations

import base64
import logging
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import allowed_roots, get_settings
from app.extract import ExtractionFailed, extract_frames
from app.security import PathSecurityError, resolve_allowed_path
from app.select import FrameSelectionError, select_frames

logger = logging.getLogger(__name__)

app = FastAPI(title="video-dairy-sampler")

#: Per-ffmpeg-invocation timeout for one /sample request (seconds).
#: "The whole request" is bounded by the two passes of ``extract_frames``,
#: each of which is capped at this value; the selection/encoding phase is
#: sub-second for the bounded frame budget, so this is the effective bound.
SAMPLE_TIMEOUT_SECONDS = 120.0


# ---------------------------------------------------------------------------
# Contract models (fixed; the backend client codes against these)
# ---------------------------------------------------------------------------


class SampleRequest(BaseModel):
    """Request body for ``POST /sample`` (extra fields are ignored)."""

    model_config = ConfigDict(extra="ignore")

    video_path: str = Field(..., min_length=1)
    probe_fps: float = Field(default=2.0, gt=0)
    min_frames: int = Field(default=4, ge=1)
    max_frames: int = Field(default=120, ge=1)
    max_edge: int | None = Field(default=640, ge=1)
    jpeg_quality: int = Field(default=85, ge=1, le=100)
    mode: Literal["uniform", "dedupe", "event"] = "uniform"
    dedupe_phash_distance: int | None = Field(default=None, ge=0)
    window_pre_seconds: float = Field(default=5.0, ge=0)
    window_post_seconds: float = Field(default=10.0, ge=0)
    max_event_windows: int = Field(default=4, ge=1)


class FrameOut(BaseModel):
    """One selected frame in the ``/sample`` response."""

    model_config = ConfigDict(extra="ignore")

    t_seconds: float
    jpeg_base64: str


class SampleResponse(BaseModel):
    """Response body for ``POST /sample``."""

    model_config = ConfigDict(extra="ignore")

    frames: list[FrameOut]
    elapsed_ms: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """Report liveness and the service version (always 200)."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": settings.version,
    }


@app.post("/sample", response_model=SampleResponse)
def sample(request: SampleRequest) -> SampleResponse:
    """Sample frames from a whitelisted video and return base64 JPEGs.

    Error mapping:
        - 400: path outside ``SAMPLER_ALLOWED_ROOTS`` / relative / ``..`` /
          symlink escape / non-file, or any schema/validation error, or
          ``FrameSelectionError`` (budget below the minimum).
        - 422: ``ExtractionFailed`` — the video cannot be decoded (corrupt
          file, decode error, ffmpeg timeout; the detail carries the ffmpeg
          stderr tail) **or** the source resolution is outside the supported
          2560x1440 box without an explicit ``max_edge`` downscale opt-in
          (the detail names the source size, the box and the remedy).
        - 500: unexpected errors; a generic message, traceback logged.
    """
    try:
        video_path = resolve_allowed_path(request.video_path, allowed_roots())
    except PathSecurityError as exc:
        logger.warning("Rejected /sample request: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = extract_frames(
            video_path,
            probe_fps=request.probe_fps,
            window_pre_seconds=request.window_pre_seconds,
            window_post_seconds=request.window_post_seconds,
            max_event_windows=request.max_event_windows,
            jpeg_quality=request.jpeg_quality,
            max_edge=request.max_edge,
            timeout_seconds=SAMPLE_TIMEOUT_SECONDS,
        )
    except ExtractionFailed as exc:
        logger.warning("Extraction failed for %s: %s", request.video_path, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        selected = select_frames(
            result.frames,
            result.candidates,
            budget=request.max_frames,
            min_frames=request.min_frames,
            mode=request.mode,
            dedupe_phash_distance=request.dedupe_phash_distance,
        )
    except FrameSelectionError as exc:
        logger.warning("Frame selection rejected for %s: %s", request.video_path, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # The JPEGs are already final-size: extract_frames scaled them in ffmpeg to
    # the max_edge budget (never upscaled), so we only base64-encode here.
    frames = [
        FrameOut(
            t_seconds=frame.t_seconds,
            jpeg_base64=base64.b64encode(frame.jpeg_bytes).decode("ascii"),
        )
        for frame in selected
    ]
    logger.info(
        "Sampled %d frames (%d candidates) from %s in %d ms",
        len(frames),
        len(result.candidates),
        request.video_path,
        result.elapsed_ms,
    )
    return SampleResponse(
        frames=frames,
        elapsed_ms=result.elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Map schema/validation errors to 400 (contract: validation = 400)."""
    logger.warning("Validation error on %s %s: %s", request.method, request.url.path, exc.errors())
    return JSONResponse(status_code=400, content={"detail": _jsonable_errors(exc.errors())})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """500 with a generic message; the traceback is logged, never leaked."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


def _jsonable_errors(errors: list[dict[str, object]]) -> list[dict[str, object]]:
    """Strip non-serialisable entries (e.g. exception objects in ``ctx``)."""
    clean: list[dict[str, object]] = []
    for error in errors:
        item = {k: v for k, v in error.items() if k != "ctx"}
        ctx = error.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {k: str(v) for k, v in ctx.items()}
        clean.append(item)
    return clean


def run() -> None:
    """Run the service on the configured host and port."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    logger.info(
        "Starting frame sampler on %s:%d (version=%s)",
        settings.host,
        settings.port,
        settings.version,
    )
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
