"""On-disk frame-set cache for the image-sampling path.

A repeated analysis of the same video file must never re-invoke the
sampler: the complete :class:`~src.services.frame_sampling.types.FrameSet`
(JPEG payloads + timestamps) is persisted under

    <root>/frames/<path_hash>/<content_signature>/<policy_key>/
        frames.json
        frame-000001.jpg
        ...

so a second analysis of the same file under the same policy and plan is
a pure disk read. The layout is structural invalidation: each changed
input produces a **different directory**, and stale entries are simply
orphaned (never deleted eagerly — a later ``put`` overwrites the matching
entry):

- a different video path -> different ``path_hash`` (sha256 of the
  absolute path);
- a different file content (size / head / tail) -> different
  ``content_signature`` (:func:`src.services.frame_sampling.fingerprint
  .content_signature`). The signature is a path component on purpose:
  a rewritten file with the same size and mtime still misses;
- a different policy field, or a different ``plan.frames`` /
  ``plan.tokens_per_image`` / ``plan.budget_tokens`` /
  ``plan.clamp_reason`` -> different ``policy_key``.

``get`` only reads disk — it never touches the sampler. A missing,
truncated, corrupt or malformed ``frames.json`` (and a missing JPEG for
a listed frame) is a clean **miss**: the entry is deliberately not
deleted, the next :meth:`FrameSetCache.put` overwrites it. A
structurally unusable cache root (it exists but is not a directory) is
raised as :class:`OSError`, never a silent miss — there is no degraded
path that fabricates or drops frames (ADR 0014: image mode has no
fallback chain).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import os
import shutil
from pathlib import Path
from typing import Any

from src.services.frame_sampling.budget import FramePlan
from src.services.frame_sampling.fingerprint import content_signature
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import FrameSample, FrameSet

logger = logging.getLogger(__name__)

CACHE_VERSION: int = 1
FRAMES_JSON_NAME: str = "frames.json"
FRAME_FILE_TEMPLATE: str = "frame-%06d.jpg"
FRAMES_DIR_NAME: str = "frames"


def _is_finite_number(value: Any) -> bool:
    """True for finite int/float (bools rejected) — manifest field checks."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


class FrameSetCache:
    """Disk-backed cache of sampled frame sets (path + content + policy keyed).

    ``root`` is the base directory (in production
    ``settings.PLAYBACK_CACHE_ROOT``); entries live under
    ``<root>/frames/<path_hash>/<content_signature>/<policy_key>/``.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------------
    # Key computation (public so tests / ops tooling can recompute the layout)
    # ------------------------------------------------------------------

    @staticmethod
    def path_hash(video_path: str) -> str:
        """sha256 hex of the absolute video path (stable across cwd changes)."""
        return hashlib.sha256(os.path.abspath(video_path).encode("utf-8")).hexdigest()

    @staticmethod
    def policy_key(policy: FramePolicy, plan: FramePlan) -> str:
        """sha256 hex over the normalized policy JSON + the plan's sampling inputs.

        Any change to a policy field, or to ``plan.frames`` /
        ``plan.tokens_per_image`` / ``plan.budget_tokens`` /
        ``plan.clamp_reason``, yields a new key (a new cache directory).
        """
        payload = {
            "policy": dataclasses.asdict(policy),
            "frames": plan.frames,
            "tokens_per_image": plan.tokens_per_image,
            "budget_tokens": plan.budget_tokens,
            "clamp_reason": plan.clamp_reason,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        *,
        video_path: str,
        policy: FramePolicy,
        plan: FramePlan,
    ) -> FrameSet | None:
        """Return the cached :class:`FrameSet` for (path, content, policy, plan).

        Disk reads only — never touches the sampler. Returns ``None``
        (a miss) when the entry is absent, when ``frames.json`` is
        missing / truncated / corrupt / malformed, or when a JPEG listed
        in the manifest is missing. A stale entry is deliberately **not**
        deleted: a subsequent :meth:`put` overwrites it.

        Raises:
            OSError: the cache root exists but is not a directory
                (misconfiguration — surfaced loudly, never a silent miss).
        """
        self._validate_root()
        entry_dir = self._entry_dir(video_path, policy, plan)
        manifest = self._read_manifest(entry_dir / FRAMES_JSON_NAME)
        if manifest is None:
            return None

        # Contract: manifests written before the slim frame set may still
        # carry ``candidates`` / ``duration_seconds`` keys; they are
        # ignored, and only the keys below are ever read.
        frames_raw = self._parse_manifest_frames(manifest)
        if frames_raw is None:
            logger.warning(
                "frame cache entry %s has a malformed manifest shape; treating as miss",
                entry_dir,
            )
            return None

        elapsed = manifest.get("elapsed_ms")
        if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0:
            logger.warning(
                "frame cache entry %s has invalid elapsed_ms=%r; treating as miss",
                entry_dir,
                elapsed,
            )
            return None

        frames: list[FrameSample] = []
        for item in frames_raw:
            jpeg_path = entry_dir / item["file"]
            if not jpeg_path.is_file():
                logger.warning(
                    "frame cache entry %s is missing %s; treating as miss",
                    entry_dir,
                    item["file"],
                )
                return None
            frames.append(
                FrameSample(
                    t_seconds=float(item["t_seconds"]),
                    jpeg_bytes=jpeg_path.read_bytes(),
                    reason=item["reason"],
                )
            )
        return FrameSet(
            frames=tuple(frames),
            source="cache",
            elapsed_ms=elapsed,
        )

    def put(
        self,
        *,
        video_path: str,
        policy: FramePolicy,
        plan: FramePlan,
        frame_set: FrameSet,
    ) -> None:
        """Persist ``frame_set`` under its cache entry (atomic tmp + ``os.replace``).

        Raises:
            ValueError: ``frame_set`` carries no frames — a zero-frame
                image-mode payload is useless, and caching it would shield
                broken sampler output from re-sampling on every retry.
            OSError: the cache root exists but is not a directory, or the
                filesystem is unwritable — surfaced loudly; there is no
                degraded path that pretends the cache write succeeded.
        """
        if not frame_set.frames:
            raise ValueError("refusing to cache an empty frame set")
        self._validate_root()
        entry_dir = self._entry_dir(video_path, policy, plan)
        entry_dir.mkdir(parents=True, exist_ok=True)
        for index, sample in enumerate(frame_set.frames):
            self._atomic_write(entry_dir / (FRAME_FILE_TEMPLATE % (index + 1)), sample.jpeg_bytes)

        manifest = {
            "version": CACHE_VERSION,
            "source": "sampler",
            "elapsed_ms": frame_set.elapsed_ms,
            "frames": [
                {
                    "t_seconds": sample.t_seconds,
                    "reason": sample.reason,
                    "file": FRAME_FILE_TEMPLATE % (index + 1),
                }
                for index, sample in enumerate(frame_set.frames)
            ],
        }
        self._atomic_write(
            entry_dir / FRAMES_JSON_NAME,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    def purge(self, video_path: str) -> None:
        """Best-effort removal of every cache entry for one video path.

        Failures are logged and swallowed — purge is a convenience helper,
        and orphaned entries are harmless (they simply never hit again).
        """
        target = self._root / FRAMES_DIR_NAME / self.path_hash(video_path)
        if not target.exists():
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            logger.warning("frame cache purge failed for %s: %s", target, exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _validate_root(self) -> None:
        """A root that exists but is not a directory is a loud misconfiguration."""
        if self._root.exists() and not self._root.is_dir():
            raise NotADirectoryError(f"frame cache root is not a directory: {self._root}")

    def _entry_dir(self, video_path: str, policy: FramePolicy, plan: FramePlan) -> Path:
        return (
            self._root
            / FRAMES_DIR_NAME
            / self.path_hash(video_path)
            / content_signature(video_path)
            / self.policy_key(policy, plan)
        )

    def _read_manifest(self, manifest_path: Path) -> dict[str, Any] | None:
        """Read + minimally validate ``frames.json``; ``None`` means a clean miss."""
        try:
            raw = manifest_path.read_bytes()
        except FileNotFoundError:
            return None
        except IsADirectoryError:
            logger.warning(
                "frame cache manifest %s is a directory; treating as miss", manifest_path
            )
            return None
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            logger.warning(
                "frame cache manifest %s is corrupt or truncated (%s); treating as miss",
                manifest_path,
                exc,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "frame cache manifest %s is not a JSON object; treating as miss", manifest_path
            )
            return None
        if data.get("version") != CACHE_VERSION:
            logger.warning(
                "frame cache manifest %s has unknown version %r; treating as miss",
                manifest_path,
                data.get("version"),
            )
            return None
        return data

    @staticmethod
    def _parse_manifest_frames(data: dict[str, Any]) -> list[dict[str, Any]] | None:
        """The ``frames`` list; ``None`` when any entry is malformed."""
        frames_raw = data.get("frames")
        if not isinstance(frames_raw, list):
            return None
        for item in frames_raw:
            if (
                not isinstance(item, dict)
                or not _is_finite_number(item.get("t_seconds"))
                or item["t_seconds"] < 0
                or not isinstance(item.get("reason"), str)
                or not isinstance(item.get("file"), str)
                or not item["file"]
                or Path(item["file"]).name != item["file"]
            ):
                return None
        return frames_raw

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        """Write ``data`` via a same-directory tmp file + ``os.replace``.

        On any failure the tmp file is removed, so a crashed write never
        leaves a ``*.tmp`` behind.
        """
        tmp = target.with_name(target.name + ".tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(data)
            os.replace(tmp, target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


__all__ = ["FrameSetCache"]
