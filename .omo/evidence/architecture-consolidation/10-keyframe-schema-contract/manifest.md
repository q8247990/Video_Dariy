# Todo 10 — Drop the retired video-preprocess / keyframe columns from the LLMProvider contract

## Goal

Finish the keyframe pipeline cleanup started by Todo 9 (commit `852338d`): retire the
configuration surface (`video_preprocess_mode` / `video_keyframe_target_n` /
`video_keyframe_jpeg_quality`) from the LLMProvider model, Pydantic schema, FastAPI
endpoints, frontend types, and PostgreSQL schema. The migration is irreversible; the
recovery path is restoring the verified pre-upgrade backup.

The keyframe code path was already deleted in Todo 9. This Todo closes the loop on
the persistence/API/UI surface so no row, view, or future write can re-introduce
the capability.

## Changed files (`git status --porcelain` snapshot)

```
 M src/models/llm_provider.py                                              (3 columns removed)
 M src/schemas/llm_provider.py                                             (3 fields removed, extra='ignore')
 M frontend/src/types/api.ts                                                (Provider type: 3 fields removed)
 M tests/unit/test_llm_provider_schema.py                                  (safety-net rewritten)
 M tests/unit/test_provider_key_crypto.py                                  (3 kwargs removed from fixtures)
 M tests/unit/test_analyzer_raw_mp4_payload.py                             (legacy-keyframe assertion rewritten)
?? alembic/versions/20260902_0018_drop_llm_provider_keyframe_columns.py     (irreversible contract migration)
?? tests/integration/test_llm_provider_drop_columns_postgres.py            (4 PG integration cases)
```

### Modifications

| File | Summary |
| --- | --- |
| `src/models/llm_provider.py` | Dropped the three `Mapped[...]` column declarations and their `server_default` values. The model now exposes only the durable columns that survive the contract migration. |
| `src/schemas/llm_provider.py` | Removed the three Pydantic fields and their validators; added `model_config = ConfigDict(extra="ignore")` on `LLMProviderBase` so legacy payloads carrying the retired keys are silently ignored (no new 400/422). Docstring pins the contract: extra keys are dropped before they can reactivate the capability. |
| `frontend/src/types/api.ts` | `Provider` type no longer carries `video_preprocess_mode` / `video_keyframe_target_n` / `video_keyframe_jpeg_quality` (and the raw_mp4-only comment). `ProviderCreate` / `ProviderUpdate` never had them. |
| `tests/unit/test_llm_provider_schema.py` | Rewritten as the post-contract safety net: asserts (a) the schema has no longer declares the fields, (b) legacy payloads with any combination of the three keys are silently ignored, (c) every previously-rejected legacy value (e.g. `Keyframe`, out-of-range integers, type-mismatched ints) is now ignored instead of raising. Parametrized over the full legacy value matrix. |
| `tests/unit/test_provider_key_crypto.py` | Removed the three kwargs from the `LLMProvider(...)` fixtures so the model construction matches the new column set. |
| `tests/unit/test_analyzer_raw_mp4_payload.py` | The previous `test_schema_rejects_keyframe_preprocess_mode` is inverted to `test_schema_silently_ignores_legacy_keyframe_fields`: with the schema field gone, the contract is "ignore" rather than "reject", and the test pins that the value cannot leak into the serialized payload. |

### Adds

| File | Purpose |
| --- | --- |
| `alembic/versions/20260902_0018_drop_llm_provider_keyframe_columns.py` | Irreversible contract migration. `down_revision = "20260826_0017"` (the previous head). `upgrade()` runs a schema preflight against `information_schema.columns` (refuses to proceed when any of the three columns is already absent, so a half-applied or out-of-band-cleaned database cannot silently no-op), then `op.drop_column` for each. `downgrade()` raises `NotImplementedError("irreversible migration - restore from verified DB backup")`. Module docstring spells out the backup-first requirement and the worker-version contract prerequisite (Todo 9 must already be deployed everywhere before this migration is run). |
| `tests/integration/test_llm_provider_drop_columns_postgres.py` | Four PG integration cases: empty DB upgrade drops columns; pre-migration head + legacy row + upgrade drops columns and preserves the row; preflight refuses when one column is already absent; downgrade is rejected with the contract message. Uses a function-scoped disposable schema so it can stage `upgrade 20260826_0017` then `upgrade head` without colliding with the session-scoped migrated engine. |

## Residual mentions (intentional)

- `src/schemas/llm_provider.py` docstring: explains the legacy-keyframe contract
  for future maintainers (so a reader does not "fix" the extra='ignore' policy back
  to 'forbid').
- `alembic/versions/20260902_0018_drop_llm_provider_keyframe_columns.py`:
  the migration legitimately references the column names it drops.
- `frontend/test-results/` (untracked): out of scope, must stay untouched per
  AGENTS.md / plan instructions.

`grep -rn "video_preprocess_mode\|video_keyframe_target_n\|video_keyframe_jpeg_quality" src/models/ src/schemas/ src/api/ frontend/src/types/api.ts alembic/versions/`
returns only the two files above.

## Verification commands and outputs

### `python3 -m alembic heads`

```
20260902_0018 (head)
```

Single head; no branch divergence.

### `python3 -m alembic upgrade head` (against `postgres:16-alpine`)

```
INFO  [alembic.runtime.migration] Running upgrade 20260826_0016 -> 20260826_0017, Make media availability and source aggregate deletion explicit.
INFO  [alembic.runtime.migration] Running upgrade 20260826_0017 -> 20260902_0018, drop llm_provider video_preprocess / keyframe columns (contract cleanup).
```

Post-upgrade `information_schema.columns` for `llm_provider` no longer contains
any of the three retired columns; the 20 surviving columns match the model.

### `python3 -m pytest tests/unit -q`

```
365 passed in 3.72s
```

(Up from the 289 baseline quoted in README because earlier Tods added suites; the
365 is what `pytest` reports after this Todo.)

### `python3 -m pytest tests/architecture -q`

```
9 passed in 0.77s
```

### `ruff check .`

```
All checks passed!
```

### `mypy src`

```
Success: no issues found in 188 source files
```

### `python3 -m pytest -m postgres -v` (PostgreSQL 16)

```
tests/integration/test_llm_provider_drop_columns_postgres.py::test_empty_database_upgrade_drops_three_columns PASSED
tests/integration/test_llm_provider_drop_columns_postgres.py::test_seeded_database_upgrade_drops_three_columns_and_keeps_rows PASSED
tests/integration/test_llm_provider_drop_columns_postgres.py::test_migration_preflight_refuses_when_columns_already_absent PASSED
tests/integration/test_llm_provider_drop_columns_postgres.py::test_migration_downgrade_is_irreversible PASSED
... (17 other postgres tests) ...
21 passed, 402 deselected in 18.07s
```

## Backup-restore recovery verification

Performed end-to-end against `postgres:16-alpine` to prove the recovery path
called out in the migration docstring:

1. `createdb backup_demo`, `alembic upgrade 20260826_0017` (pre-migration head).
2. Inserted a `legacy-row` with `video_preprocess_mode='raw_mp4'`,
   `video_keyframe_target_n=120`, `video_keyframe_jpeg_quality=88`.
3. `pg_dump -U postgres --schema=public --no-owner --no-acl -d backup_demo`
   → `/tmp/opencode/tod10-backup/backup_demo_pre_migration.sql` (57 KB).
4. `alembic upgrade head`: columns dropped, `legacy-row` survives.
5. `dropdb backup_demo; createdb backup_recovery; psql -d backup_recovery < backup.sql`.
6. Post-restore verification:

```
SELECT version_num FROM alembic_version;       -- 20260826_0017
SELECT column_name FROM information_schema.columns
 WHERE table_schema='public' AND table_name='llm_provider'
   AND column_name LIKE 'video_%' ORDER BY column_name;
-- video_keyframe_jpeg_quality
-- video_keyframe_target_n
-- video_preprocess_mode
SELECT id, provider_name, video_preprocess_mode,
       video_keyframe_target_n, video_keyframe_jpeg_quality
  FROM llm_provider;
-- 3 | legacy-row | raw_mp4 | 120 | 88
```

The three columns and the legacy row's per-column values are recovered intact
after restoring the verified pre-migration backup.

## Acceptance against plan §Todo 10

- [x] `src/models/llm_provider.py` 无三列
- [x] `src/schemas/llm_provider.py` 无三字段(Pydantic)且 `extra='ignore'`
- [x] `src/api/v1/endpoints/llm_providers.py` 不读写三字段(POST/PATCH 通过 schema dump 自动传播)
- [x] `frontend/src/types/api.ts` 不含三字段
- [x] 新增 alembic version `20260902_0018_drop_llm_provider_keyframe_columns.py`:
      `downgrade()` 抛 `NotImplementedError("irreversible migration - restore from verified DB backup")`,
      `upgrade()` 先查询 information_schema 确认三列存在,然后 `op.drop_column`
- [x] `alembic upgrade head` 单 head 成功(`alembic heads` 只输出一个新 head)
- [x] PG 集成测试:空库 + 旧库升级均成功;旧库的三列升级后从 `information_schema.columns` 不可见
- [x] POST /api/v1/providers 旧 payload(含已删除字段)按既有 Pydantic policy 处理,本次**未引入新 400/422**
- [x] `ruff check .` / `mypy src` 通过
- [x] 单元测试继续通过(365 项)
- [x] 备份可恢复到删除前 revision(见上文 backup-restore verification)
- [x] `tests/unit/test_llm_provider_schema.py` 安全网已重写为新契约下的兜底断言

## Notes for the follow-up tasks

- The migration's `extra='ignore'` policy on `LLMProviderBase` is the explicit
  contract for "legacy payloads are silently dropped" — do **not** flip it back
  to `extra='forbid'` (would 400 existing clients) or to `extra='allow'` (would
  let attackers keep deprecated keys in persisted payloads).
- The `frag_keyframe+empty_moov` flag in `src/services/ffmpeg_utils.py` is an
  ffmpeg MP4 muxer switch unrelated to this contract; do not touch it.
