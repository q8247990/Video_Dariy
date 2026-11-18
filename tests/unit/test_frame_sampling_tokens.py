"""Unit tests for ``src/services/frame_sampling/tokens.py``.

Covers the single-image token estimation and the aspect-preserving long-edge
scaling used by the image-frame-sampling plan (todo 4).

Formula source: ``docs/research/2026-11-17-image-input-and-frame-sampling.md``
§2.4 (``tokens ~= (w/32) * (h/32)``) and §7.2.1
(``w, h = fit_long_edge(src_w, src_h, max_edge, align=32)`` then
``tok_per_img = (w // 32) * (h // 32)``).
"""

from __future__ import annotations

import pytest

from src.services.frame_sampling.tokens import (
    estimate_image_tokens,
    estimate_image_tokens_for_source,
    fit_long_edge,
)


class TestEstimateImageTokens:
    """The ``(w // 32) * (h // 32)`` conversion from research §2.4."""

    @pytest.mark.parametrize(
        ("w", "h", "expected"),
        [
            (640, 480, 300),
            (960, 544, 510),
            (1280, 704, 880),
            (1920, 1080, 1980),
        ],
    )
    def test_required_table(self, w: int, h: int, expected: int) -> None:
        assert estimate_image_tokens(w, h) == expected

    def test_sub_tile_edge_clamps_to_one_token(self) -> None:
        # An edge below one 32px tile contributes 0 tiles; the estimate is
        # floored at 1 token rather than 0.
        assert estimate_image_tokens(16, 16) == 1
        assert estimate_image_tokens(31, 4096) == 1


class TestFitLongEdge:
    """Aspect-preserving long-edge fit with 32px snap-down, never upscale."""

    def test_landscape_downscale(self) -> None:
        # 1080 * 640 / 1920 = 360 -> snapped down to 352 (11 * 32).
        assert fit_long_edge(1920, 1080, 640) == (640, 352)

    def test_never_upscales_small_source(self) -> None:
        # Long edge (640) is already <= max_edge (1600): keep the source size,
        # only align. 480 is a multiple of 32, so the result is exactly (640, 480).
        result_w, result_h = fit_long_edge(640, 480, 1600)
        assert (result_w, result_h) == (640, 480)
        assert result_w <= 640 and result_h <= 480
        assert result_w % 32 == 0 and result_h % 32 == 0

    def test_portrait_aspect_error_within_align(self) -> None:
        # Portrait 1080x1920 at max_edge=640: each fitted edge must stay within
        # one align step (32px) of the exact aspect-preserving scale.
        fitted_w, fitted_h = fit_long_edge(1080, 1920, 640)
        exact_w = 1080 * 640 / 1920  # 360.0
        exact_h = 1920 * 640 / 1920  # 640.0
        assert abs(fitted_w - exact_w) <= 32
        assert abs(fitted_h - exact_h) <= 32
        # The fitted size is deterministic under the snap-down rule: 360 -> 352.
        assert (fitted_w, fitted_h) == (352, 640)
        assert max(fitted_w, fitted_h) == 640


class TestEstimateImageTokensForSource:
    def test_returns_fitted_size_and_tokens(self) -> None:
        size, tokens = estimate_image_tokens_for_source(1920, 1080, 640)
        assert size == (640, 352)
        assert tokens == estimate_image_tokens(*size)
        # 640 // 32 = 20, 352 // 32 = 11 -> 220 tokens.
        assert tokens == 220


class TestValidation:
    @pytest.mark.parametrize(
        ("func", "kwargs"),
        [
            (fit_long_edge, {"src_w": 0, "src_h": 1080, "max_edge": 640}),
            (fit_long_edge, {"src_w": 1920, "src_h": 0, "max_edge": 640}),
            (fit_long_edge, {"src_w": 1920, "src_h": 1080, "max_edge": 0}),
            (fit_long_edge, {"src_w": -1, "src_h": 1080, "max_edge": 640}),
            (fit_long_edge, {"src_w": 1920, "src_h": -1080, "max_edge": 640}),
            (fit_long_edge, {"src_w": 1920, "src_h": 1080, "max_edge": -640}),
            (fit_long_edge, {"src_w": 1920, "src_h": 1080, "max_edge": 640, "align": 0}),
            (fit_long_edge, {"src_w": 1920, "src_h": 1080, "max_edge": 640, "align": -32}),
            (fit_long_edge, {"src_w": True, "src_h": 1080, "max_edge": 640}),
            (fit_long_edge, {"src_w": 1920, "src_h": False, "max_edge": 640}),
            (estimate_image_tokens, {"w": 0, "h": 480}),
            (estimate_image_tokens, {"w": 640, "h": 0}),
            (estimate_image_tokens, {"w": -640, "h": 480}),
            (estimate_image_tokens, {"w": True, "h": 480}),
            (estimate_image_tokens_for_source, {"src_w": 0, "src_h": 1080, "max_edge": 640}),
            (
                estimate_image_tokens_for_source,
                {"src_w": 1920, "src_h": 1080, "max_edge": 0},
            ),
            (
                estimate_image_tokens_for_source,
                {"src_w": 1920, "src_h": 1080, "max_edge": True},
            ),
        ],
    )
    def test_rejects_invalid_input(self, func, kwargs) -> None:
        with pytest.raises(ValueError):
            func(**kwargs)
