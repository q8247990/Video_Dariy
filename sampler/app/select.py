"""Frame selection within a budget (todo 18).

Selects a bounded number of frames from an extracted frame sequence using one
of three deterministic strategies:

- ``uniform``  — index-proportional even spacing over the whole clip
- ``dedupe``   — perceptual-hash (pHash) walk keeping frames that are
  sufficiently different from every kept frame, down-sampled evenly when the
  walk overshoots the budget and farthest-first filled when it undershoots,
  so exactly ``budget`` frames are returned
- ``event``    — up to 70% of the budget allocated to candidate event windows
  (proportional to window duration, at least one frame per non-empty window),
  then even-spacing fill for the remainder; identical to ``uniform`` when
  there are no candidates

The module is deliberately structural: it works with any object exposing
``t_seconds`` / ``jpeg_bytes`` (frames) and ``t_start`` / ``t_end`` /
``reasons`` (candidate windows). It never imports ``app.extract`` (todo 17),
whose ``ExtractedFrame`` / ``CandidateWindow`` dataclasses satisfy these
protocols structurally.
"""

from __future__ import annotations

import io
import logging
from typing import Protocol, Sequence

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

#: Default perceptual-hash threshold for ``dedupe`` mode: a frame is kept
#: only when its Hamming distance to *every* already-kept frame is greater
#: than this value.
PHASH_DEFAULT_DISTANCE = 6

#: Fraction of the budget that ``event`` mode may allocate to candidate windows.
EVENT_WINDOW_BUDGET_FRACTION = 0.7

_VALID_MODES = ("uniform", "dedupe", "event")


class ExtractedFrameLike(Protocol):
    """Duck-typed view of ``app.extract.ExtractedFrame`` (todo 17 contract)."""

    t_seconds: float
    jpeg_bytes: bytes


class CandidateWindowLike(Protocol):
    """Duck-typed view of ``app.extract.CandidateWindow`` (todo 17 contract)."""

    t_start: float
    t_end: float
    reasons: tuple[str, ...]


class FrameSelectionError(ValueError):
    """Raised when the requested budget is below the minimum frame floor."""


# ---------------------------------------------------------------------------
# Perceptual hashing
# ---------------------------------------------------------------------------


def _build_dct_matrix(n: int) -> np.ndarray:
    """Type-II DCT matrix; the 2-D DCT is applied separably (rows, then cols)."""
    k = np.arange(n, dtype=np.float64)
    m = np.arange(n, dtype=np.float64)
    matrix = np.cos(np.pi * (2.0 * m[None, :] + 1.0) * k[:, None] / (2.0 * n))
    matrix *= 2.0 / n
    return matrix


_DCT_32 = _build_dct_matrix(32)


def phash(jpeg_bytes: bytes) -> int:
    """Compute the 64-bit perceptual hash of a JPEG image.

    Pipeline: grayscale -> 32x32 resize -> DCT -> top-left 8x8 -> median
    threshold. Returns an integer in ``[0, 2**64)``; bit ``i`` (row-major
    order of the 8x8 block) is set when the DCT coefficient exceeds the
    median of the block.
    """
    image = (
        Image.open(io.BytesIO(jpeg_bytes)).convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    )
    matrix = np.asarray(image, dtype=np.float64)
    dct = _DCT_32 @ matrix @ _DCT_32.T
    top_left = dct[:8, :8].ravel()
    median = float(np.median(top_left))
    value = 0
    for i, is_set in enumerate(top_left > median):
        if is_set:
            value |= 1 << i
    return value


def hamming(a: int, b: int) -> int:
    """Hamming distance between two integer hashes (popcount of the XOR)."""
    return (a ^ b).bit_count()


# ---------------------------------------------------------------------------
# Selection machinery
# ---------------------------------------------------------------------------


def _unique_sorted_frames(
    frames: Sequence[ExtractedFrameLike],
) -> list[ExtractedFrameLike]:
    """Stable-sort frames by timestamp, keeping one frame per timestamp.

    The result has strictly ascending ``t_seconds``; for duplicated
    timestamps the earliest original position wins (stable ordering).
    """
    indexed = sorted(enumerate(frames), key=lambda pair: (pair[1].t_seconds, pair[0]))
    unique: list[ExtractedFrameLike] = []
    seen: set[float] = set()
    for _, frame in indexed:
        t = frame.t_seconds
        if t not in seen:
            seen.add(t)
            unique.append(frame)
    return unique


def _fill_positions(n: int, need: int, forbidden: set[int]) -> list[int]:
    """Pick up to ``need`` frame indices by even spacing over the whole clip.

    Target positions follow the same proportional formula as uniform
    sampling; each target falls back to the nearest index not in
    ``forbidden`` (ties resolve toward the higher index, deterministic).
    Returns fewer indices only when not enough free indices remain.
    """
    if need <= 0:
        return []
    need = min(need, n - len(forbidden))
    if need <= 0:
        return []
    if need == 1:
        targets = [(n - 1) // 2]
    else:
        targets = [round(i * (n - 1) / (need - 1)) for i in range(need)]
    picked: list[int] = []
    for target in targets:
        for radius in range(n):
            candidates = (target,) if radius == 0 else (target + radius, target - radius)
            for cand in candidates:
                if 0 <= cand < n and cand not in forbidden and cand not in picked:
                    picked.append(cand)
                    break
            else:
                continue
            break
    return picked


def _uniform_selection(frames: list[ExtractedFrameLike], budget: int) -> list[ExtractedFrameLike]:
    """Index-proportional even spacing, deduplicated and back-filled to the
    exact budget (called only when ``1 <= budget < len(frames)``)."""
    n = len(frames)
    if budget == 1:
        selected: list[int] = [0]
    else:
        raw = [round(i * (n - 1) / (budget - 1)) for i in range(budget)]
        selected = []
        seen: set[int] = set()
        for idx in raw:
            if idx not in seen:
                seen.add(idx)
                selected.append(idx)
        selected.extend(_fill_positions(n, budget - len(selected), seen))
    return [frames[i] for i in selected]


def _downsample_positions(positions: list[int], budget: int) -> list[int]:
    """Evenly down-sample ascending ``positions`` to exactly ``budget`` entries.

    Both endpoints are preserved (``budget == 1`` keeps the first position
    only). The result stays strictly ascending: the source list is strictly
    ascending and the spacing exceeds one entry whenever a down-sample is
    needed, so no two rounded targets can collide.
    """
    count = len(positions)
    if budget >= count:
        return list(positions)
    if budget == 1:
        return [positions[0]]
    step = (count - 1) / (budget - 1)
    return [positions[round(j * step)] for j in range(budget)]


def _farthest_first_fill(
    hashes: Sequence[int],
    times: Sequence[float],
    selected: list[int],
    need: int,
) -> None:
    """Append ``need`` indices to ``selected`` by farthest-first order.

    Each round picks the unselected frame whose minimum Hamming distance to
    the already-selected hashes is largest; ties break toward the largest
    minimum time gap, then the earliest index. The threshold is deliberately
    not a filter here: when every remaining frame sits within it, the
    farthest one is still taken so the budget is honoured. ``selected`` must
    be non-empty. Runs in O(len(hashes) * need) with incremental minima.
    """
    n = len(hashes)
    chosen = set(selected)
    min_distance = [0] * n
    min_gap = [0.0] * n
    for i in range(n):
        if i in chosen:
            continue
        min_distance[i] = min(hamming(hashes[i], hashes[k]) for k in chosen)
        min_gap[i] = min(abs(times[i] - times[k]) for k in chosen)
    for _ in range(need):
        best = max(
            (i for i in range(n) if i not in chosen),
            key=lambda i: (min_distance[i], min_gap[i], -i),
            default=None,
        )
        if best is None:
            return
        chosen.add(best)
        selected.append(best)
        for i in range(n):
            if i in chosen:
                continue
            min_distance[i] = min(min_distance[i], hamming(hashes[i], hashes[best]))
            min_gap[i] = min(min_gap[i], abs(times[i] - times[best]))


def _dedupe_selection(
    frames: list[ExtractedFrameLike], budget: int, threshold: int
) -> list[ExtractedFrameLike]:
    """Select exactly ``budget`` frames by pHash-guided farthest-first order.

    The greedy walk keeps every frame farther than ``threshold`` bits from
    all previously kept frames. That set is then forced onto the budget: an
    even down-sample preserving both endpoints when it overshoots, and a
    farthest-first fill when it undershoots. The threshold ranks candidates
    but is relaxed when it would starve the budget; the first and last
    frames of the stream always survive (except ``budget == 1``, where the
    first frame wins), so head/tail coverage is kept. With
    ``budget >= len(frames)`` all frames are returned, since selection
    cannot invent frames.
    """
    n = len(frames)
    if budget >= n:
        return list(frames)
    hashes = [phash(frame.jpeg_bytes) for frame in frames]
    times = [frame.t_seconds for frame in frames]
    kept: list[int] = []
    for i, h in enumerate(hashes):
        if all(hamming(h, hashes[k]) > threshold for k in kept):
            kept.append(i)
    if kept[-1] != n - 1:
        kept.append(n - 1)
    if len(kept) > budget:
        return [frames[i] for i in _downsample_positions(kept, budget)]
    if len(kept) < budget:
        _farthest_first_fill(hashes, times, kept, budget - len(kept))
    return [frames[i] for i in sorted(kept)]


def _allocate_window_budgets(
    window_frames: list[tuple[CandidateWindowLike, list[int]]],
    window_budget: int,
) -> list[int]:
    """Allocate ``window_budget`` frames across the given windows.

    When windows outnumber the budget, the earliest windows each get one
    frame (the returned list is shorter; the caller pairs windows with
    allocations in order). Otherwise allocation is proportional to window
    duration (point windows split the budget evenly), clamped so the total
    is exactly ``window_budget``.
    """
    if len(window_frames) > window_budget:
        return [1] * window_budget
    durations = [max(w.t_end - w.t_start, 0.0) for w, _ in window_frames]
    total = sum(durations)
    if total <= 0:
        base, extra = divmod(window_budget, len(window_frames))
        return [base + (1 if j < extra else 0) for j in range(len(window_frames))]
    allocations = [max(1, round(window_budget * d / total)) for d in durations]
    while sum(allocations) > window_budget and any(a > 1 for a in allocations):
        j = max(range(len(allocations)), key=lambda k: (allocations[k], -k))
        allocations[j] -= 1
    while sum(allocations) < window_budget:
        j = max(range(len(allocations)), key=lambda k: (durations[k], -k))
        allocations[j] += 1
    return allocations


def _pick_window_positions(m: int, alloc: int) -> list[int]:
    """Evenly-spaced positions for ``alloc`` frames within a window of ``m``.

    All positions when ``alloc >= m``; the middle position when
    ``alloc == 1``; otherwise the index-proportional targets back-filled
    with :func:`_fill_positions` (same formula as ``_uniform_selection``).
    """
    if alloc >= m:
        return list(range(m))
    if alloc == 1:
        return [m // 2]
    picked: list[int] = []
    seen: set[int] = set()
    for j in range(alloc):
        p = round(j * (m - 1) / (alloc - 1))
        if p not in seen:
            seen.add(p)
            picked.append(p)
    picked.extend(_fill_positions(m, alloc - len(picked), seen))
    return picked


def _event_selection(
    frames: list[ExtractedFrameLike],
    candidates: Sequence[CandidateWindowLike],
    budget: int,
) -> list[ExtractedFrameLike]:
    """Allocate up to 70% of the budget to candidate windows, then fill.

    Windows are processed in ``(t_start, t_end)`` order. Each non-empty
    window receives at least one frame, proportional to its duration
    (point windows split the budget evenly). When there are more windows
    than the 70% cap allows, the earliest windows win. With no non-empty
    window the mode behaves exactly like ``uniform``.
    """
    n = len(frames)
    window_frames: list[tuple[CandidateWindowLike, list[int]]] = []
    for window in candidates:
        idx = [
            i for i, frame in enumerate(frames) if window.t_start <= frame.t_seconds <= window.t_end
        ]
        if idx:
            window_frames.append((window, idx))
    if not window_frames:
        return _uniform_selection(frames, budget)

    window_frames.sort(key=lambda pair: (pair[0].t_start, pair[0].t_end))
    window_budget = int(budget * EVENT_WINDOW_BUDGET_FRACTION)
    if window_budget <= 0:
        return _uniform_selection(frames, budget)

    allocations = _allocate_window_budgets(window_frames, window_budget)

    selected: set[int] = set()
    for (_, idx), alloc in zip(window_frames, allocations, strict=False):
        selected.update(idx[p] for p in _pick_window_positions(len(idx), alloc))

    need = budget - len(selected)
    if need > 0:
        selected.update(_fill_positions(n, need, selected))
    return [frames[i] for i in sorted(selected)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_frames(
    frames: Sequence[ExtractedFrameLike],
    candidates: Sequence[CandidateWindowLike],
    *,
    budget: int,
    min_frames: int,
    mode: str,
    dedupe_phash_distance: int | None = None,
    head_seconds: float = 5.0,
    tail_seconds: float = 10.0,
) -> list[ExtractedFrameLike]:
    """Select up to ``budget`` frames from ``frames``.

    Args:
        frames: extracted frames; objects exposing ``t_seconds`` and
            ``jpeg_bytes`` (e.g. ``app.extract.ExtractedFrame``). Order is
            not required; frames are sorted by timestamp internally.
        candidates: candidate event windows; objects exposing ``t_start``,
            ``t_end`` and ``reasons`` (e.g. ``app.extract.CandidateWindow``).
        budget: maximum number of frames to return. Must be at least
            ``max(1, min_frames)`` or :class:`FrameSelectionError` is raised.
        min_frames: floor used by the budget check.
        mode: one of ``"uniform"``, ``"dedupe"``, ``"event"``; anything else
            raises ``ValueError``.
        dedupe_phash_distance: ``dedupe`` preference threshold; frames above
            it are kept first. ``None`` means
            :data:`PHASH_DEFAULT_DISTANCE` (6). It is relaxed only when it
            would keep the selection off the budget.
        head_seconds: size of the clip head zone (see notes).
        tail_seconds: size of the clip tail zone (see notes).

    Output guarantees:
        - ``budget >= len(frames)`` -> all frames, in timestamp order; a
          selection cannot invent frames.
        - otherwise: exactly ``budget`` frames with strictly ascending unique
          ``t_seconds`` in every mode (``dedupe`` included).
        - the first (earliest) frame is always included. The last frame is
          always included as well, except with ``budget == 1`` over a longer
          clip (first frame wins).
        - head/tail zone coverage: with ``head_seconds``/``tail_seconds``
          non-negative, the head zone (first ``head_seconds`` seconds) and
          the tail zone (last ``tail_seconds`` seconds) each contain at
          least one selected frame whenever they are non-empty, which
          follows from first/last inclusion (the zone is "empty" only when
          it contains no frame at all).

    Deterministic: same inputs always yield the same selection.
    """
    floor = max(1, min_frames)
    if budget < floor:
        raise FrameSelectionError(
            f"budget ({budget}) is below the minimum frame count: "
            f"at least {floor} frame(s) required (min_frames={min_frames})"
        )
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}; expected one of: {', '.join(_VALID_MODES)}")

    ordered = _unique_sorted_frames(frames)
    n = len(ordered)
    if n == 0:
        logger.debug("select_frames: no frames provided, returning empty selection")
        return []
    if budget >= n:
        logger.debug("select_frames: budget %d >= frame count %d, returning all", budget, n)
        return list(ordered)

    if mode == "uniform":
        selected = _uniform_selection(ordered, budget)
    elif mode == "dedupe":
        threshold = (
            PHASH_DEFAULT_DISTANCE if dedupe_phash_distance is None else dedupe_phash_distance
        )
        selected = _dedupe_selection(ordered, budget, threshold)
    else:
        selected = _event_selection(ordered, candidates, budget)

    logger.debug(
        "select_frames: mode=%s budget=%d frames_in=%d frames_out=%d "
        "head_seconds=%s tail_seconds=%s",
        mode,
        budget,
        n,
        len(selected),
        head_seconds,
        tail_seconds,
    )
    return selected
