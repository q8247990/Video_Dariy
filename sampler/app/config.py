"""Environment-driven configuration for the frame sampler service.

The sampler is a standalone, out-of-process service. It must never talk to
Postgres or Redis; every runtime knob comes from an environment variable so
a single image can serve every deployment.
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 18080
DEFAULT_VERSION = "0.1.0"


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the process-wide sampler configuration."""

    host: str
    port: int
    allowed_roots_raw: str
    model_path_raw: str
    version: str


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read the environment once and cache the result for the process lifetime.

    Callers that need a fresh read (tests, configuration reloads) must
    invoke ``get_settings.cache_clear()`` first.
    """
    port_raw = os.environ.get("SAMPLER_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_raw)
    except ValueError:
        logger.warning(
            "SAMPLER_PORT=%r is not an integer; falling back to %d", port_raw, DEFAULT_PORT
        )
        port = DEFAULT_PORT
    return Settings(
        host=os.environ.get("SAMPLER_HOST", DEFAULT_HOST),
        port=port,
        allowed_roots_raw=os.environ.get("SAMPLER_ALLOWED_ROOTS", ""),
        model_path_raw=os.environ.get("SAMPLER_MODEL_PATH", ""),
        version=os.environ.get("SAMPLER_VERSION", DEFAULT_VERSION),
    )


def allowed_roots() -> list[Path]:
    """Normalise ``SAMPLER_ALLOWED_ROOTS`` into a de-duplicated list of absolute paths.

    The variable is a colon-separated list of absolute paths. An empty or
    unset value means nothing is allowed. Relative entries are ignored with
    a warning because the sampler only ever operates on absolute paths.
    """
    raw = get_settings().allowed_roots_raw
    roots: list[Path] = []
    for entry in raw.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        path = Path(entry)
        if not path.is_absolute():
            logger.warning("Ignoring relative SAMPLER_ALLOWED_ROOTS entry: %s", entry)
            continue
        roots.append(path.resolve())
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def model_path() -> Path | None:
    """Return the configured model file path, or ``None`` when unset or missing."""
    raw = get_settings().model_path_raw
    if not raw:
        return None
    path = Path(raw)
    if not path.exists():
        return None
    return path
