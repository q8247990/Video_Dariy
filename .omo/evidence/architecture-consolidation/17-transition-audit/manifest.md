# Todo 17 — PipelineTransitionLog append-only audit

## Outcome

Every successful compare-and-set transition through
`src.services.pipeline_state.transition_session` /
`transition_task_log` now writes **two** audit records in the
caller's session — the existing `TaskLog.detail_json["transition"]`
embed (kept for backward compat) and one new
`pipeline_transition_log` row (the durable history).

The new table makes three guarantees structural rather than
application-level:

1. The two legal `aggregate_type` literals ("VideoSession",
   "TaskLog") are pinned by a CHECK constraint so a typo'd
   aggregate type — or a future transition-log row from a
   *different* aggregate kind (`outbox_event`,
   `daily_summary_generation_attempt`) — is rejected at INSERT
   time rather than silently polluting the audit stream.
2. The append-only invariant is enforced by the writer contract:
   `src.application.transition_log.repository.record` issues
   exactly one INSERT per call, and the CAS helpers in
   `pipeline_state.py` only invoke it when their UPDATE matches
   ≥1 row. A lost race (`from_status` mismatch or
   `cancel_requested` short-circuit) writes zero audit rows, by
   construction.
3. The FK to `task_log` is `ON DELETE SET NULL`, so the 7-day
   `task_log` cleanup surrogate (`DELETE FROM task_log`) NULLs
   the correlation id rather than cascading the audit row away
   (`CASCADE` would erase the diagnostics) or blocking the
   cleanup task (`RESTRICT` would wedge the cleanup). The audit
   history outlives the run that produced it.

`SESSION_ALLOWED_TRANSITIONS` and `TASK_LOG_ALLOWED_TRANSITIONS`
are byte-identical to the pre-Todo-17 versions — the audit is a
*witness* to the CAS, not a validator of it.

## Files changed

### New

- `src/models/pipeline_transition_log.py` — append-only
  `PipelineTransitionLog` model. 11 columns (12 with the
  inherited `updated_at`), two indexes (`aggregate_type`,
  `aggregate_id`, `occurred_at` for replay; `task_log_id`,
  `occurred_at` for correlation), one CHECK constraint on
  `aggregate_type`, one FK to `task_log` (`ON DELETE SET NULL`).
- `src/application/transition_log/__init__.py` — re-exports
  `AGGREGATE_TYPE_VIDEO_SESSION` / `AGGREGATE_TYPE_TASK_LOG` /
  `record`.
- `src/application/transition_log/repository.py` — single
  function `record(db, *, aggregate_type, aggregate_id,
  from_status, to_status, reason, source, task_log_id=None)`.
  Adds one row to the caller's session; no commit. Pre-INSERT
  contract check (raise `ValueError` on unknown
  `aggregate_type` / empty `source`) so typos are loud.
- `alembic/versions/20260902_0021_add_pipeline_transition_log.py`
  — `revision="20260902_0021", down_revision="20260902_0020"`,
  irreversible (`downgrade()` raises `NotImplementedError`).
  Preflight asserts `task_log` exists, mirrors the preflight
  shape from `20260902_0020` so a database without `task_log`
  cannot silently no-op this revision. Same-field contract as
  the model plus the inherited `updated_at`.
- `tests/unit/test_pipeline_transition_log_sqlite.py` — 11 tests
  covering the standalone `record` writer contract, the CAS
  helpers' success / lost-race / illegal-transition paths, and
  the FK `ON DELETE SET NULL` behaviour against an in-memory
  SQLite engine with the `PRAGMA foreign_keys = ON` connection
  hook.
- `tests/integration/test_pipeline_transition_log_postgres.py` —
  5 tests against real PostgreSQL:
  - `full_session_state_chain_replays_via_audit_rows`
  - `cancel_then_complete_no_false_audit`
  - `task_log_7day_cleanup_keeps_audit_rows`
  - `transition_audit_is_append_only_no_update_semantics`
  - `fk_to_task_log_set_null_on_delete_pg`

### Modified

- `src/models/__init__.py` — registers
  `PipelineTransitionLog` between `OutboxEvent` and
  `SessionAnalysisCheckpoint` (alphabetical).
- `src/db/base.py` — same registration for `Base.metadata`.
- `src/services/pipeline_state.py` — `transition_session` and
  `transition_task_log` now also call
  `src.application.transition_log.record(...)` on the
  *success* branch (i.e. when the CAS UPDATE matches ≥1 row).
  The inline `task_log.detail_json["transition"]` embed is
  unchanged. A lost race / illegal transition /
  `cancel_requested` short-circuit writes **no** audit row.
  `SESSION_ALLOWED_TRANSITIONS` / `TASK_LOG_ALLOWED_TRANSITIONS`
  sets are unchanged.
- `tests/architecture/test_dependency_boundaries.py` — adds
  `"src.application.transition_log"` to the
  `services_no_application_import` rule's
  `excluded_target_subprefixes`. Rationale: `transition_log` is
  a write-only helper called from `pipeline_state` — the same
  shape of shared helper as the existing
  `"src.application.ports"` carve-out (by-design importable from
  every layer; not an orchestrator / use case / schema). Both the
  `_detect_violations()` detector and the git-diff guard
  `test_no_new_violations_introduced` honour the carve-out by
  the existing `_is_excluded(...)` helper.
- 6 existing test fixtures — `tests/unit/test_pipeline_state.py`
  / `test_analyzer_raw_mp4_payload.py` / `test_analyzer_task.py`
  / `test_task_maintenance.py` / `test_task_control_endpoints.py`
  / `test_task_retry_use_case.py` — add **one** line to the
  in-memory SQLite bootstrap:
  `PipelineTransitionLog.__table__.create(bind=engine)`. These
  fixtures exercise `transition_*` indirectly via the analyzer /
  task-maintenance / task-control paths, so the audit table must
  exist for the CAS helpers to insert into. No test assertion
  changes; the fixture addition is a mechanical maintenance
  change.

## Verification

```bash
ruff check .
ruff format --check src tests
mypy src
```

- `ruff check .` → `All checks passed!`
- `ruff format --check src tests` →
  `308 files already formatted`
- `mypy src` → `Success: no issues found in 212 source files`

```bash
DATABASE_URL=postgresql+psycopg://postgres:123456@localhost:5432/home_monitor \
  python3 -m pytest tests/unit -q
```

- `530 passed in 5.25s` (`519` pre-existing + `11` new audit
  unit tests).

```bash
DATABASE_URL=postgresql+psycopg://postgres:123456@localhost:5432/home_monitor \
  python3 -m pytest -m postgres -q
```

- `68 passed, 567 deselected in 14.17s` (`63` pre-existing +
  `5` new audit PG tests).

```bash
DATABASE_URL=postgresql+psycopg://postgres:123456@localhost:5432/home_monitor \
  python3 -m pytest tests/architecture -q
```

- `9 passed in 0.76s`. The dependency-boundary test still passes
  — the `transition_log` carve-out in
  `services_no_application_import` follows the same shape as
  the existing `ports` carve-out.

```bash
DATABASE_URL=postgresql+psycopg://postgres:123456@localhost:5432/home_monitor \
  python3 -m alembic heads
```

- `20260902_0021 (head)` — single head. The new migration
  `20260902_0021_add_pipeline_transition_log` extends
  `20260902_0020` (the prior head from Todo 15) and is the only
  active revision.

## Acceptance criteria (Todo 17 acceptance)

> 每个成功状态转换恰有一条审计

Pinned by `test_transition_session_writes_exactly_one_audit_row_on_success`
/ `test_transition_task_log_writes_audit_row_on_success` (SQLite)
and the audit query in
`test_full_session_state_chain_replays_via_audit_rows` (PG):
three CAS applications write three rows in `occurred_at` order.

> CAS 失败不写审计

Pinned by `test_transition_session_lost_race_writes_no_audit_row`
/ `test_transition_task_log_lost_race_writes_no_audit_row` and
`test_cancel_then_complete_no_false_audit`: a lost race (CAS
matches zero rows) or `cancel_requested` short-circuit writes zero
audit rows.

> 非法转换不写审计

Pinned by `test_illegal_session_transition_raises_and_writes_no_audit`
/ `test_illegal_task_transition_raises_and_writes_no_audit`: the
state-machine check (`(from, to) not in ALLOWED_TRANSITIONS`) is
performed *before* any database work, so an illegal transition
cannot leave a half-written audit row.

> 清理 TaskLog 后审计仍可查询

Pinned by `test_cleanup_task_log_nullifies_fk_but_keeps_transition_audit`
(SQLite) and `test_task_log_7day_cleanup_keeps_audit_rows` /
`test_fk_to_task_log_set_null_on_delete_pg` (PG): the FK is
`ON DELETE SET NULL`, so the audit row's `task_log_id` goes to
`NULL` rather than the row being deleted.

> 已有允许边集合不变

`SESSION_ALLOWED_TRANSITIONS` /
`TASK_LOG_ALLOWED_TRANSITIONS` are byte-identical to the
pre-Todo-17 versions — no edge added or removed. Pinned
implicitly by every existing
`tests/unit/test_pipeline_state.py` test
(`test_transition_session_applies_every_legal_edge` /
`test_transition_task_log_applies_every_legal_edge`) passing
without changes.

## Notes / decisions

- **Two audits per CAS, not one.** The existing
  `task_log.detail_json["transition"]` embed is kept for
  backward compat (a worker log still carries a one-step
  audit). The new `pipeline_transition_log` row is the durable
  history that outlives the 7-day `task_log` cleanup. Both
  writes happen in the caller's session so a single `commit()`
  persists them atomically.
- **`Base` auto-injects `updated_at` on every model.** A naive
  model + migration that documents 11 columns without
  `updated_at` will fail at PG INSERT time because SQLAlchemy's
  default INSERT includes the inherited columns. Migration
  `20260902_0021` declares `updated_at` explicitly (matching
  every other new table in the project) and the model docstring
  notes that `created_at` / `updated_at` are inherited from
  `Base`.
- **Carve-out, not `KNOWN_VIOLATIONS`.**
  `services_no_application_import` was extended (in the same
  shape as the existing `src.application.ports` carve-out) so a
  new file under `src/application/transition_log` is reachable
  from `src/services/pipeline_state.py` without a
  `KNOWN_VIOLATIONS` exception. The git-diff guard
  `test_no_new_violations_introduced` honours the carve-out via
  the existing `_is_excluded(...)` helper. `KNOWN_VIOLATIONS`
  remains the empty tuple — any new forbidden import fails
  loudly.
- **Cascade / `RESTRICT` / `SET NULL`** follows the
  `llm_usage_log` / `daily_summary_generation_attempt` (Todo
  15) pattern. The 7-day `task_log` cleanup surrogate in
  `test_task_log_7day_cleanup_keeps_audit_rows` deletes the
  parent and asserts the audit row survives with `task_log_id`
  NULL.
- **Single commit** `feat: persist pipeline transition history`
  contains the new model + migration + repository + package
  init, the wired-in `pipeline_state` helpers, the unit + PG
  tests, the notepad appends, the fixture maintenance lines
  across 6 existing test files, the boundary-rule carve-out, and
  this manifest.
