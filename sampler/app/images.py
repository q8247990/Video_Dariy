"""JPEG frame post-processing for the frame sampler service (todo 19).

After ``app.select`` picks the frames to return, each frame is downscaled so
its long edge fits the caller's ``max_edge`` budget and re-encoded at the
requested JPEG quality. Frames that already fit are passed through untouched
— the sampler never upscales.
"""

from __future__ import annotations

import io
import logging

from PIL import Image

logger = logging.getLogger(__name__)


def resize_jpeg(jpeg_bytes: bytes, max_edge: int, jpeg_quality: int) -> bytes:
    """Return a JPEG whose long edge is at most ``max_edge``.

    The aspect ratio is preserved (the short edge is scaled by the same
    factor, rounded to whole pixels, minimum 1). When the input already
    satisfies the constraint the original bytes are returned unchanged, so
    no re-encoding generation is added and no upscaling can happen.

    Args:
        jpeg_bytes: A complete JPEG file (SOI through EOI).
        max_edge: Maximum allowed length of the longer edge in pixels (>= 1).
        jpeg_quality: Target JPEG quality 1..100, used only when a resize
            actually happens.

    Returns:
        JPEG bytes, decoded as ``JPEG``-format by Pillow.

    Raises:
        ValueError: when ``max_edge`` is below 1 or ``jpeg_bytes`` is not a
            decodable image.
    """
    if max_edge < 1:
        raise ValueError(f"max_edge must be >= 1, got {max_edge}")
    image = Image.open(io.BytesIO(jpeg_bytes))
    image.load()
    width, height = image.size
    long_edge = max(width, height)
    if long_edge <= max_edge:
        logger.debug(
            "Frame %dx%d already within max_edge=%d, passing through", width, height, max_edge
        )
        return jpeg_bytes

    if long_edge == width:
        new_size = (max_edge, max(1, round(height * max_edge / width)))
    else:
        new_size = (max(1, round(width * max_edge / height)), max_edge)

    resized = image.convert("RGB").resize(new_size, Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    resized.save(buffer, format="JPEG", quality=jpeg_quality)
    logger.debug(
        "Resized frame %dx%d -> %dx%d (max_edge=%d, quality=%d)",
        width,
        height,
        new_size[0],
        new_size[1],
        max_edge,
        jpeg_quality,
    )
    return buffer.getvalue()
