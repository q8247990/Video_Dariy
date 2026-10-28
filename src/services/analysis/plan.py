"""Analysis work planning: one plan item per video file plus the deterministic run id."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from src.services.session_analysis_video import SubChunk, build_file_sub_chunks

# --- chunk_plan.py ---
PLAN_VERSION: int = 2


# --- chunk_plan.py ---
@dataclass(frozen=True)
class SubChunkPlan:
    """A single sub-chunk work item — the unit the LLM sees once."""

    chunk_index: int
    sub_chunk_index: int
    start_offset_seconds: int
    duration_seconds: int
    file_paths: tuple[str, ...]


# --- chunk_plan.py ---
@dataclass(frozen=True)
class AnalysisPlan:
    """The plan produced for one session analysis run."""

    session_id: int
    sub_chunks: tuple[SubChunkPlan, ...]
    analysis_run_id: str

    def sub_chunk_count(self) -> int:
        return len(self.sub_chunks)


# --- chunk_plan.py ---
def _file_identities(paths: Iterable[str]) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for path in paths:
        info: dict[str, Any] = {"path": path}
        if Path(path).exists():
            stat = Path(path).stat()
            info.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        identities.append(info)
    return identities


# --- chunk_plan.py ---
def analysis_run_id_for_paths(paths: Iterable[str]) -> str:
    """Stable per-run fingerprint hashed from file identities + plan version.

    Only path + size + mtime_ns participate; reading the file contents
    would be prohibitive and is unnecessary — every other layer
    (sub-chunk fingerprint, checkpoint writer, event-record replace)
    hashes a sha256 over the file paths themselves, so two different
    run_ids can only arise from genuinely different on-disk material or
    a planning-scheme change.
    """
    payload = {"plan_version": PLAN_VERSION, "files": _file_identities(paths)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


# --- chunk_plan.py ---
def _plan_item(sub_chunk: SubChunk) -> SubChunkPlan:
    return SubChunkPlan(
        chunk_index=sub_chunk.chunk_index,
        sub_chunk_index=sub_chunk.sub_chunk_index,
        start_offset_seconds=sub_chunk.start_offset_seconds,
        duration_seconds=sub_chunk.duration_seconds,
        file_paths=tuple(sub_chunk.file_paths),
    )


# --- chunk_plan.py ---
def build_analysis_plan(db: Session, session_id: int) -> AnalysisPlan:
    """Build the file-level work plan for one session.

    Each session video file becomes exactly one :class:`SubChunkPlan`.
    The DB session is used only to read the session's ordered files.
    """
    sub_chunks = [_plan_item(sub) for sub in build_file_sub_chunks(db, session_id)]
    paths = [path for item in sub_chunks for path in item.file_paths]
    return AnalysisPlan(
        session_id=session_id,
        sub_chunks=tuple(sub_chunks),
        analysis_run_id=analysis_run_id_for_paths(paths),
    )


# --- chunk_plan.py ---
def sub_chunk_fingerprint(
    *,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str,
    sub_chunk: SubChunkPlan,
) -> str:
    """Per-sub-chunk content fingerprint used as a write condition.

    Combines the two prompt halves, the sub-chunk's start offset and
    duration, the exact list of files in the sub-chunk, and a sha256 of
    the raw ``data:video/mp4`` payload. Any of these changing is a
    signal the previous checkpoint was produced by an LLM call that no
    longer matches the current input and must be invalidated.
    """
    payload = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "video_sha256": sha256(video_data_url.encode()).hexdigest(),
        "start_offset_seconds": sub_chunk.start_offset_seconds,
        "duration_seconds": sub_chunk.duration_seconds,
        "file_paths": list(sub_chunk.file_paths),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "AnalysisPlan",
    "PLAN_VERSION",
    "SubChunkPlan",
    "analysis_run_id_for_paths",
    "build_analysis_plan",
    "sub_chunk_fingerprint",
]
