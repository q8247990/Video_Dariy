"""Frame sampler service application (scaffold, todo 15).

Out-of-process service that will sample frames from surveillance recordings
for the main pipeline. This scaffold only exposes ``/healthz``; the
``/sample`` endpoint arrives in todo 19. The process always starts and
answers 200 even when the model file is missing; callers check
``model_loaded`` to decide whether sampling is possible.
"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from app.config import get_settings, model_path

logger = logging.getLogger(__name__)

app = FastAPI(title="video-dairy-sampler")


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """Report liveness, model availability and the service version."""
    settings = get_settings()
    return {
        "status": "ok",
        "model_loaded": model_path() is not None,
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
