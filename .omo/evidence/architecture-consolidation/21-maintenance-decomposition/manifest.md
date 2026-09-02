# Todo 21 - Separate task lifecycle and maintenance policies

## Goal

Wave 5 decomposes the two remaining pre-Wave-5 monoliths in the
task lifecycle / maintenance area:

* `src/services/task_dispatch_control.py` (415 LOC) - the task
  lifecycle policy surface (dedupe key construction, active-row
  lookup, pending claim, worker-bind idempotency, lease renewal,
  terminal finalization, user-cancellation observation, HOT/FULL
  precedence).
* `src/tasks/task_maintenance.py` (~373 LOC) - the heartbeat body
  (hot-build dispatch + lease recovery + missing-file sweep +
  retention cleanup).

The monoliths are split into discrete, independently-testable
policy modules.

**Task lifecycle - `src/services/dispatch/`:**

* `constants.py` - the `TERMINAL_TASK_STATUSES` bucket the
  worker-bind path uses to short-circuit stale broker messages.
* `dedupe.py` - `build_dedupe_key` / `ensure_dict_detail` and the
  private dedupe-key normalisation seam shared by claim and bind.
* `claim.py` - `create_pending_task_log` (Postgres `INSERT ... 
  ON CONFLICT DO NOTHING` with a partial unique index, SQLite
  SELECT-then-INSERT fallback), `find_duplicate_active_task`,
  `is_singleton_task_running`.
* `lease.py` - `renew_task_lease`.
* `finalize.py` - `finalize_task_log` / `finalize_cancelled_task_log`.
* `cancel.py` - `get_task_log_for_update` / `is_task_cancel_requested`
  / `ensure_task_not_cancelled` / `TaskCancellationRequested`.
* `deferred.py` - `record_deferred_hot_scan`.
* `worker_bind/` - `bind_or_create_running_task_log` plus the
  `_branches.py` / `_state.py` private helpers that keep the
  C901 budget of the public entry under control.
* `__init__.py` - re-exports every public name so
  `from src.services.task_dispatch_control import X` call sites
  keep working unchanged.
* `supersede_active_hot_scan` (HOT-over-FULL precedence) lives here.

**Maintenance policies - `src/services/maintenance/`:**

* `hot_scheduling.py` - `dispatch_hot_builds` (per-source HOT scan
  dispatch).
* `running_lease_recovery.py` - `recover_timed_out_tasks` (the
  "crashed worker" detector for RUNNING rows whose lease expired).
* `unleased_recovery.py` - `recover_orphan_pending_tasks` /
  `recover_unleased_pending_tasks` (PENDING rows never picked up).
* `analysis_recovery.py` - `resume_lost_analysis` (the
  analysis-specific lease-expiry re-queue / TIMEOUT finalization).
* `retention.py` - `cleanup_old_task_logs` (the 7-day terminal log
  sweep).
* `missing_file.py` - `mark_missing_video_files` (the hourly
  source-file sweep).
* `constants.py` - the per-task-type timeout buckets
  (`HOT_BUILD_TIMEOUT_SECONDS`, `FULL_BUILD_TIMEOUT_SECONDS`,
  `ANALYSIS_BASE_TIMEOUT_SECONDS`, `DAILY_SUMMARY_TIMEOUT_SECONDS`,
  `ANALYSIS_RETRY_GRACE_SECONDS`).
* `__init__.py` - re-exports every public name.

**Slim aggregators / façades:**

* `src/tasks/task_maintenance.py` - the heartbeat body is now one
  call per policy module; the legacy private helpers
  (`_dispatch_hot_builds`, `_recover_timed_out_tasks`,
  `_recover_orphan_pending_tasks`, `_mark_missing_video_files`,
  `_cleanup_old_task_logs`) are kept as re-exports so the existing
  `tests/unit/test_task_maintenance.py` and
  `tests/unit/test_tasks_di.py` keep passing unchanged.
* `src/services/task_dispatch_control.py` - thin facade re-exporting
  `src.services.dispatch`.

**Tests (new, Todo-21-scoped):**

* `tests/unit/test_dispatch_maintenance_policies.py` - the SQLite
  unit suite (dedupe / claim / worker-bind / lease / finalize /
  cancel / hot-scheduling / running-lease / unleased / orphan /
  missing-file / retention / heartbeat aggregation).
* `tests/integration/test_dispatch_maintenance_postgres.py` - the
  PG suite (partial unique index, transactional heartbeat boundary,
  concurrency, retention, outbox dispatch path).
* `tests/architecture/test_dependency_boundaries.py` - updated to
  allow the new service layers to import the frozen command DTOs
  (`src.application.pipeline.commands`) and the outbox enqueue seam
  (`src.application.outbox.contracts` / `.enqueue`) for the
  same reason `src.application.transition_log` is exempted.

## Fixes (Todo 21 focused work)

Two production regressions were found and fixed while bringing the
full unit + `-m postgres` suites green:

### Fix 1 - recovery must commit TIMEOUT

`resume_lost_analysis` (lease-expired recoverable run) left the
`TaskLog` row `RUNNING` instead of `TIMEOUT` (and left the
`VideoSession` `ANALYZING` instead of `SEALED`), breaking
`tests/unit/test_task_maintenance.py::test_worker_loss_after_checkpoint_auto_resumes_exactly_once`.

**Root cause:** `transition_task_log` /
`src.services.pipeline_state.py` issued the CAS UPDATE with
`synchronize_session=False`, then called `db.refresh(task_log)` to
load the post-transition status into the identity map. That
combination left the ORM object's `status` attribute tracked as
"unchanged" relative to the session's pre-refresh committed value,
so the **follow-up flush** (triggered by `resume_lost_analysis`
setting `finished_at` / `message` / `recovery_attempt` on the same
object before `db.commit()`) re-wrote the row without the CAS'd
status -- silently reverting it to `RUNNING`. The same hazard
existed in `transition_session` for `VideoSession.analysis_status`.

**Fix:** in `transition_task_log`, replace `db.refresh(task_log)`
with an explicit in-memory `task_log.status = to_status`, which
marks `status` as genuinely dirty so the flush persists it. In
`transition_session`, sync the identity-mapped
`VideoSession.analysis_status = to_status` after the CAS. Both are
one-line, behaviour-preserving changes in `src/services/pipeline_state.py`.
The CAS UPDATE (the real concurrency guard) is untouched, so the
lost-race semantics are unchanged.

This is the fix that makes the recovery end with `TaskLog` =
`TIMEOUT` and `VideoSession` = `SEALED` committed atomically.

### Fix 2 - PG concurrency dedup

`tests/integration/test_dispatch_maintenance_postgres.py::test_concurrent_orphan_recovery_creates_one_recovery`
is order-dependent: in the full `-m postgres` run it observed
`sum(results) == 9` instead of `3`.

**Root cause:** `recover_unleased_pending_tasks` (and the
lease-expired PENDING sweep in `recover_orphan_pending_tasks`)
selected PENDING rows with a plain `SELECT` and then flipped them to
`TIMEOUT` in memory -- **no row lock**. Under true concurrency two
sweeps can read the same PENDING rows, both flip them to TIMEOUT,
and both inflate their returned counter. Whether `sum(results)` is
`3` or `9` is timing-dependent scheduling luck, not determinism.

**Fix (production, real bug):** add `.with_for_update()` to both
sweep queries so each sweep claims rows exclusively (`SELECT ... FOR
UPDATE` on PostgreSQL; a no-op on the SQLite test dialect). A
concurrent sweep now blocks until the leader commits, then re-reads
the TIMEOUT rows (excluded by the `status == PENDING` filter) and
returns `0`, giving the deterministic `sum(results) == 3`. This
matches the codebase's established locking convention (used in
`src/application/summary_attempt/repository.py`, `analysis/claim.py`).

**Test isolation (test file):** the shared `postgres_migrated_engine`
is session-scoped, so `task_log` rows left by earlier tests leak into
broad sweeps. Added an autouse `_reset_policy_sweep_tables` fixture
to the Todo-21 integration file that deletes `outbox_event` then
`task_log` (FK-safe order) before each test, keeping the module
order-independent.

## Verification

All gates green against the local PostgreSQL (`home_monitor`):

* `ruff check .` - all checks passed
* `ruff format --check src tests` - 367 files already formatted
* `mypy src` - success, 263 source files
* `DATABASE_URL=... python3 -m pytest tests/unit -q` - 623 passed
* `DATABASE_URL=... python3 -m pytest -m postgres -q` - 96 passed
  (including the formerly order-dependent concurrency test)
* `python3 -m pytest tests/integration/test_task_maintenance_postgres.py
  tests/unit/test_task_maintenance.py tests/unit/test_task_dispatch_binding.py -q`
  - 17 passed
* `python3 -m pytest tests/architecture -q` - 9 passed
* `alembic head` - 20260902_0021 (no migration added)

No frozen modules (`outbox/`, `summary_attempt/`, `transition_log/`)
or analyzer / summarizer / session_build stage modules were touched.
No new migration. `frontend/`, `frontend/test-results/`, and `.env`
untouched.

## Notes / decisions / learnings

* **`synchronize_session=False` CAS + `db.refresh` is a footgun.**
  A `synchronize_session=False` bulk UPDATE leaves the ORM object's
  in-session committed state untouched; the subsequent `db.refresh`
  reloads the attribute but the object can still be re-written by a
  later flush in a way that silently discards the CAS'd value. The
  safe pattern is to assign the target status to the in-memory
  object explicitly so the flush persists it. This is a general
  lesson for every state-machine transition in this codebase, not
  just the maintenance recovery path.
* **Concurrency-safety sweeps need `with_for_update()`.** A sweep
  that reads candidate rows then mutates them must take row locks,
  otherwise concurrent sweeps double-count. The "single heartbeat"
  deployment masked it; the PG concurrency spec surfaced it.
* **The task's Fix-1 hypothesis ("`applied` is False") was wrong.**
  Diagnosis showed `claimed.applied == True` (rowcount 1) -- the CAS
  succeeded. The real regression was the flush silently reverting
  the committed status, which a raw-`db.execute` trace inside the
  failing unit test exposed. Temp prints were removed after
  diagnosis per the brief.
* **Commit scope.** `src/services/pipeline_state.py` is included in
  the commit because Fix 1 lives there (it is not a frozen module;
  the transition *sets* are untouched). The Todo-21 facade /
  aggregator split, the two new test files, the architecture boundary
  update, and the two production fixes ship in one commit.

## Reversibility

Removing `src/services/dispatch/` and `src/services/maintenance/`,
the two new test files, and reverting
`src/services/task_dispatch_control.py` +
`src/tasks/task_maintenance.py` +
`tests/architecture/test_dependency_boundaries.py` +
`src/services/pipeline_state.py` to their pre-Todo-21 versions
restores the post-Todo-20 state. No DB change is introduced.
