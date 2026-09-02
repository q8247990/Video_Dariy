# ADR 0009 — Remove the disabled keyframe pipeline code, configuration, and dependency surface

- Status: accepted
- Date: 2026-09-02
- Owner: architecture-consolidation Wave 2 / Todo 9
- Related: ARCHITECTURE.md §5.2, AGENTS.md §10, plan
  `.omo/plans/architecture-consolidation.md` (Todo 9)

## Context

The session analyzer historically supported two video preprocessing
modes before the vLLM side took over frame sampling:

1. `raw_mp4` — embed the sub-chunk mp4 as `data:video/mp4;base64,...`
   together with `media_io_kwargs.video.num_frames` and let the
   vision provider sample frames uniformly.
2. `keyframe` — run client-side ffmpeg decode + MAD/pHash scoring +
   top-N JPEG assembly, then send a `data:video/jpeg;base64,...`
   payload annotated with `media_io_kwargs.video.{fps,
   total_num_frames, frames_indices, num_frames=-1}` so the model
   could reason over a hand-picked frame set.

Mode 2 was a product decision that was already disabled before this
ADR existed:

- `video_preprocess_mode` in `llm_provider.video_preprocess_mode` was
  normalized to `"raw_mp4"` and the schema validator
  (`src/schemas/llm_provider.py`) rejects any non-`raw_mp4` value at
  422 before it reaches the analyzer task.
- Migration `20260825_0011_normalize_video_preprocess_mode_to_raw_mp4.py`
  flipped every existing row, so historical data could not re-enable
  the path even via a stale provider record.
- The analyzer task already pinned `preprocess_mode = "raw_mp4"` and
  emitted a warning log if a non-raw row was encountered, so the
  keyframe `build_chunk_keyframe_payload(...)` branch was reachable
  only through direct `services` import — never through the production
  Celery task.

What remained was the dead code, the inert configuration surface
(`ANALYZER_VIDEO_KEYFRAME_*` settings), an A/B harness script, and
`keyframe_extractor.py` itself (which depends on `cv2` / `numpy` that
are intentionally not in the deployment requirements). The goal of
this ADR and Todo 9 is to remove that surface so:

- no runtime call graph or test imports cv2/numpy,
- no `ANALYZER_VIDEO_KEYFRAME_*` setting can be set to a value that
  has any effect,
- the analyzer task is provably locked to `data:video/mp4` +
  `num_frames=120` (the product decision) and any drift requires a
  deliberate edit rather than being hidden inside a disabled branch,
- `ruff format --check` has zero retained exceptions.

## Decision

1. **Delete the keyframe code surface.**

   Remove:

   - `src/services/keyframe_extractor.py`
   - `scripts/ab_keyframe_10min.py`
   - the `extract_keyframes_for_sub_chunk`, `build_chunk_keyframe_payload`,
     `ChunkKeyframePayload`, `keyframe_set` field, and `KeyframeSet`
     reference inside `src/services/session_analysis_video.py`.
   - any consumer call to those symbols (analyzer, tests).

2. **Delete the inert settings.** Remove every
   `ANALYZER_VIDEO_KEYFRAME_*` field (`PERIOD_SECONDS`,
   `MAD_THRESHOLD`, `PHASH_THRESHOLD`, `FALLBACK_TO_MP4`) from
   `src/core/config.py`.

3. **Pin `num_frames=120` as a constant, not a setting.** The value is
   a documented product decision (the vLLM sampler target), not a
   tunable. It now lives at module scope as
   `src.tasks.analyzer.RAW_MP4_NUM_FRAMES = 120`; nothing in runtime
   config can override it.

4. **Refactor the analyzer task to a single payload path.** Replace
   the dual-mode loop with one unconditional block that:
   - builds the `data:video/mp4;base64,...` URL via
     `build_chunk_video_data_url`,
   - emits `media_io_kwargs = {"video": {"num_frames": RAW_MP4_NUM_FRAMES}}`,
   - sends the user message with `{"type": "video_url", ...}` as the
     only media part.
   The task log `detail_json` no longer records `keyframe_total`,
   `keyframe_fallback`, or a `preprocess_mode` field; the field
   `media_num_frames` is added for parity.

5. **Drop the cv2 / numpy dependency surface.** Confirmed
   `requirements.in`, `requirements.txt`, `requirements.lock`, and
   `pyproject.toml` already do not install `opencv-python` or
   `numpy`. The deletion makes the import graph consistent: after
   this ADR no `src/`, `tests/`, or `scripts/` file under version
   control imports `cv2` or `numpy`.

6. **Keep the persistence layer untouched.** The Pydantic schema
   (`src/schemas/llm_provider.py`), the SQLAlchemy model
   (`src/models/llm_provider.py`), the Alembic migrations, and the
   frontend type (`frontend/src/types/api.ts`) still expose the
   `video_preprocess_mode`, `video_keyframe_target_n`, and
   `video_keyframe_jpeg_quality` fields. Removing them is a separate
   contract migration (Todo 10) that requires the verified DB backup
   ritual called out in AGENTS.md §10; this ADR explicitly leaves
   them in place for that follow-up.

7. **Lock the contract with a regression test.** Add
   `tests/unit/test_analyzer_raw_mp4_payload.py` that asserts:
   - `RAW_MP4_NUM_FRAMES == 120`,
   - the analyzer task sends a payload whose `video_url.url` starts
     with `data:video/mp4;base64,` and whose `media_io_kwargs.video
     .num_frames` equals `RAW_MP4_NUM_FRAMES`,
   - `LLMProviderBase(**, video_preprocess_mode="keyframe")` raises
     a Pydantic validation error (so the reject happens before the
     task can ever see the value),
   - the keyframe payload DTO and helpers are not on
     `src.services.session_analysis_video` or `src.tasks.analyzer`
     anymore.

## Consequences

Positive

- `pytest tests/unit` passes with zero keyframe-specific tests; the
  raw_mp4 path coverage is preserved.
- `ruff format --check src tests` returns zero reformatting work,
  clearing the last known exception (`tests/unit/test_keyframe_extractor.py`).
- The analyzer task's payload contract is now expressed as code, not
  as a comment reading "this branch is not reachable".
- No code path under version control depends on `cv2` or `numpy`,
  removing the surprise when a developer runs the backend with the
  standard deployment requirements only.
- `mypy src` and `ruff check .` continue to pass.

Costs / follow-ups

- The frontend field set is temporarily larger than what the backend
  can mutate: until Todo 10 removes the columns, Provider write
  endpoints can still echo back the three legacy values verbatim,
  even though they have no effect. Consumers must treat the values
  as deprecated read-only metadata.
- The keyframe extraction algorithm (ffmpeg single-pass decode +
  MAD/pHash + top-N JPEG) is gone for good. Any future experiment
  that needs it must reintroduce the dependency `opencv-python` and
  the entire pipeline module through a fresh ADR — there is no
  quiet fallback path.
- The Analyzer `TaskLog.detail_json` lost `keyframe_total`,
  `keyframe_fallback`, and `preprocess_mode` keys. Operators used to
  grepping those terms need to update their alerting queries.

Verification commands

```bash
python3 -m pytest tests/unit -q
ruff check .
ruff format --check src tests
mypy src
```

Recorded outputs at the time of this ADR:

```
366 passed in ~3.7s
All checks passed!
268 files already formatted
Success: no issues found in 188 source files
```

Reversibility

This change is mechanically reversible only if:

- `git log` and the commit object are still available (we keep the
  removed files recoverable via `git show <commit>`).
- `git revert <commit>` reruns cleanly on top of HEAD at the moment
  the revert is applied. It is not a runtime-rerunnable transition;
  reintroducing the keyframe pipeline requires re-installing
  `opencv-python` and `numpy` and restoring the four files in their
  prior form, which is why this ADR records the product decision
  explicitly.

Status

accepted — supersedes any prior in-code comment claiming the
keyframe branch is "kept intentionally but not reachable".
