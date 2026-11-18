"""Frame sampler service application (scaffold, todo 15 + detector, todo 16).

Out-of-process service that will sample frames from surveillance recordings
for the main pipeline. Currently it only exposes ``/healthz``; the
``/sample`` endpoint arrives in todo 19. The process always starts and
answers 200 even when the model file is missing or invalid; callers check
``model_loaded`` to decide whether sampling is possible.
"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from app.config import get_settings
from app.detector import detector_ready

logger = logging.getLogger(__name__)

app = FastAPI(title="video-dairy-sampler")


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """Report liveness, model availability and the service version.

    ``model_loaded`` is ``True`` only when the configured model file exists
    and loads into an onnxruntime session; a missing or invalid model still
    yields 200 with ``model_loaded: false`` (never raises).
    """
    settings = get_settings()
    return {
        "status": "ok",
        "model_loaded": detector_ready(settings.model_path_raw),
        "version": settings.version,
    }


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
