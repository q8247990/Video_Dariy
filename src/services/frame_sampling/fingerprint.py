"""Fingerprints for the frame-sampling stage.

Two fingerprints, both sha256 over canonical JSON / length-prefixed bytes:

- :func:`content_signature` — a *light* content signature of a video file:
  it hashes only the size, the first chunk and the last chunk, so it never
  reads the whole file. Deliberate trade-off: files with identical
  size/head/tail but different middles collide; callers must not treat it
  as a full-file integrity hash.
- :func:`frame_set_fingerprint` — fingerprint of a sampled frame set
  (positions + per-frame JPEG digests + the resolved policy + the source
  content signature), mirroring the canonical-JSON style of
  ``sub_chunk_fingerprint`` in :mod:`src.services.analysis.plan`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import struct
from collections.abc import Sequence
from pathlib import Path

from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import FrameSample, FrameSet

LENGTH_PREFIX = struct.Struct(">Q")


def content_signature(path: str | Path, *, chunk_bytes: int = 65_536) -> str:
    """Light content signature of a file: ``sha256(size || head || tail)``.

    Each part is prefixed with its big-endian 64-bit length to avoid
    boundary ambiguity. Only the first and last ``chunk_bytes`` are read;
    the file is never scanned in full. For files smaller than
    ``2 * chunk_bytes`` the head and tail windows overlap — the digest is
    still deterministic (head is read first, then tail). Missing files
    raise :class:`OSError` (e.g. :class:`FileNotFoundError`) naturally.

    Known limitation (by design): two files with identical size, head and
    tail but different middles produce the SAME signature.
    """
    file_path = Path(path)
    size = file_path.stat().st_size
    with file_path.open("rb") as fh:
        head = fh.read(chunk_bytes)
        fh.seek(max(0, size - chunk_bytes))
        tail = fh.read(chunk_bytes)

    digest = hashlib.sha256()
    digest.update(LENGTH_PREFIX.pack(size))
    digest.update(LENGTH_PREFIX.pack(len(head)))
    digest.update(head)
    digest.update(LENGTH_PREFIX.pack(len(tail)))
    digest.update(tail)
    return digest.hexdigest()


def frame_set_fingerprint(
    frames: Sequence[FrameSample] | FrameSet,
    policy: FramePolicy,
    *,
    content_signature: str,
    extra: dict[str, object] | None = None,
) -> str:
    """Fingerprint a frame set: positions + JPEG digests + policy + source.

    The payload is canonical JSON (``sort_keys=True``, compact separators)
    containing ``version`` (1), ``content_signature``, the full normalized
    policy dict (all 13 fields) and one ``[index, t_seconds, jpeg_sha256]``
    entry per frame (``t_seconds`` rounded to 3 decimals). A non-``None``
    ``extra`` mapping is included verbatim.

    Accepts both a sequence of :class:`FrameSample` and a :class:`FrameSet`
    (its ``.frames`` are used). An empty frame set raises
    :class:`ValueError`.
    """
    samples = frames.frames if isinstance(frames, FrameSet) else list(frames)
    if not samples:
        raise ValueError("cannot fingerprint an empty frame set")

    payload: dict[str, object] = {
        "version": 1,
        "content_signature": content_signature,
        "policy": dataclasses.asdict(policy),
        "frames": [
            [index, round(sample.t_seconds, 3), hashlib.sha256(sample.jpeg_bytes).hexdigest()]
            for index, sample in enumerate(samples)
        ],
    }
    if extra is not None:
        payload["extra"] = extra
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = [
    "content_signature",
    "frame_set_fingerprint",
]
