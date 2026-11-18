"""Path confinement for the frame sampler service (todo 19).

The sampler reads video files supplied by the main pipeline. Every requested
path must point at a regular file inside one of the ``SAMPLER_ALLOWED_ROOTS``
directories; anything else is rejected before a single byte is read.

``resolve_allowed_path`` is the single choke point used by ``POST /sample``.
It fails closed: relative paths, unnormalised paths containing ``..`` and
symlink targets that escape the allowed roots all raise
:class:`PathSecurityError`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


class PathSecurityError(ValueError):
    """A requested video path is outside the allowed roots or malformed.

    The message always refers to the *requested* path (what the caller sent),
    never to internal resolved locations.
    """


def resolve_allowed_path(video_path: str, roots: Sequence[Path]) -> Path:
    """Validate ``video_path`` against the allowed roots and return its resolved form.

    Rules (checked in order, fail closed):

    1. ``video_path`` must be a non-empty string.
    2. It must be an absolute path (relative paths are rejected).
    3. It must be normalised: no ``..`` components (``/a/b/../c`` is rejected
       even when it would normalise back inside a root).
    4. After ``Path.resolve()`` (which follows symlinks), the target must be
       one of ``roots`` or located below one of them. Symlinks inside a root
       that point outside it therefore escape and are rejected.
    5. The resolved target must be a regular file (directories and missing
       files are rejected).

    Args:
        video_path: Absolute, normalised path of the video file to read.
        roots: Allowed root directories (``app.config.allowed_roots()``).
            An empty sequence allows nothing.

    Returns:
        The fully resolved path (symlinks followed, normalised).

    Raises:
        PathSecurityError: when any of the rules above is violated.
    """
    if not isinstance(video_path, str) or not video_path:
        raise PathSecurityError("video_path must be a non-empty string")
    if not Path(video_path).is_absolute():
        raise PathSecurityError(f"video_path must be an absolute path, got {video_path!r}")
    if ".." in Path(video_path).parts:
        raise PathSecurityError(
            f"video_path must be normalised (no '..' components), got {video_path!r}"
        )

    resolved = Path(video_path).resolve()
    for root in roots:
        root_resolved = root.resolve()
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            if not resolved.is_file():
                raise PathSecurityError(f"video_path is not a regular file: {video_path!r}")
            return resolved

    logger.warning("Rejected video path outside allowed roots: %s", video_path)
    raise PathSecurityError(f"video_path is outside the allowed roots: {video_path!r}")
