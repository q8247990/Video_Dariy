"""Single-image token estimation and aspect-preserving long-edge scaling.

Pure integer helpers for the image-frame-sampling plan (todo 4). They predict
how many tokens one sampled JPEG will occupy once sent to a Qwen3-VL style
vision reader, and compute the aligned size a source frame should be resized
to. No image libraries are used: everything is integer math.

Formula source: ``docs/research/2026-11-17-image-input-and-frame-sampling.md``
§2.4 (``tokens ~= (w/32) * (h/32)``) and §7.2.1
(``w, h = fit_long_edge(src_w, src_h, max_edge, align=32)`` then
``tok_per_img = (w // 32) * (h // 32)``).
"""

from __future__ import annotations

#: Qwen3-VL / mtmd tile size: one token per 32x32 tile.
TILE_EDGE = 32


def _validate_positive_int(value: int, name: str) -> None:
    """Reject bool / non-positive / non-int dimensions with a clear message."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")


def _snap_down(value: int, align: int) -> int:
    """Snap ``value`` down to a multiple of ``align`` (minimum ``align``)."""

    return max(align, (value // align) * align)


def fit_long_edge(src_w: int, src_h: int, max_edge: int, align: int = TILE_EDGE) -> tuple[int, int]:
    """Scale a source frame so its long edge equals ``max_edge`` (or smaller).

    - When the long edge exceeds ``max_edge`` the frame is scaled down while
      preserving the aspect ratio (the long edge lands exactly on ``max_edge``).
    - The frame is never upscaled: a source whose long edge is already
      ``<= max_edge`` keeps its original size.
    - Each resulting edge is snapped DOWN to a multiple of ``align`` (minimum
      ``align``), matching the tile alignment the vision reader expects.

    Returns the fitted ``(width, height)``.
    """

    _validate_positive_int(src_w, "src_w")
    _validate_positive_int(src_h, "src_h")
    _validate_positive_int(max_edge, "max_edge")
    _validate_positive_int(align, "align")

    if max(src_w, src_h) > max_edge:
        if src_w >= src_h:
            w = max_edge
            h = src_h * max_edge // src_w
        else:
            h = max_edge
            w = src_w * max_edge // src_h
    else:
        w = src_w
        h = src_h

    return (_snap_down(w, align), _snap_down(h, align))


def estimate_image_tokens(w: int, h: int) -> int:
    """Estimate the tokens a ``w x h`` image occupies.

    Mirrors the Qwen3-VL / mtmd conversion: one token per 32x32 tile,
    ``max(1, (w // 32) * (h // 32))``.
    """

    _validate_positive_int(w, "w")
    _validate_positive_int(h, "h")
    return max(1, (w // TILE_EDGE) * (h // TILE_EDGE))


def estimate_image_tokens_for_source(
    src_w: int, src_h: int, max_edge: int
) -> tuple[tuple[int, int], int]:
    """Convenience: fitted size plus its token estimate for a source frame."""

    size = fit_long_edge(src_w, src_h, max_edge)
    return (size, estimate_image_tokens(size[0], size[1]))
