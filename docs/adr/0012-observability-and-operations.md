# ADR 0012 — Observability, structured logging, and the operations runbook

- Status: accepted
- Date: 2026-09-03
- Owner: architecture-consolidation Wave 6 / Todo 24
- Related: ADR 0011 (outbox / task lifecycle), ARCHITECTURE.md §5.1–§5.4 and §7,
  AGENTS.md §5–§10, the root README runbook, plan
  `.omo/plans/architecture-consolidation.md` (Todo 24).
- Predecessor: Todo 23 (readiness / worker health / migration lock /
  deployment resource governance).

## Context

Todos 11–23 shipped the durable machinery (transactional outbox,
`DailySummaryGenerationAttempt`, `PipelineTransitionLog`, livez / readyz,
the migration advisory lock, worker / beat / publisher healthchecks) but
no **operator view** of it: how does an operator observe outbox lag, task
recovery, or checkpoint progress? How does an operator correlate a single
API request to the Celery task that eventually worked on it? How does an
operator back up and restore the database before an irreversible
migration? And how do we keep secrets and filesystem paths out of logs
that will be shared and grepped?

This ADR records the decisions that answer those questions. It is the
centralized operations contract for the whole chain and supersedes the
scattered "Todo 24" pointers in earlier ADRs.

## Summary of decisions

| # | Decision | Where |
|---|----------|-------|
| 1 | Structured **JSON** logging with a correlation id threaded request → outbox → worker | `src/core/logging_config.py`, `src/main.py`, `src/core/celery_app.py` |
| 2 | **Sensitive-field redaction** (secrets + `VIDEO_ROOT_PATH` paths) applied at the log boundary | `src/core/logging_config.py::redact` |
| 3 | The outbox **`event_id` is the correlation key**; it links API dispatch, publish, and worker execution | `celery_dispatcher`, `publisher`, `celery_app` signal |
| 4 | `/metrics` surface for outbox lag, task recovery, checkpoint progress | `src/main.py`, `src/db/metrics.py` |
| 5 | Heartbeat persists its recovery counters to `AppRuntimeState` | `src/tasks/task_maintenance.py` |
| 6 | Checksummed **backup / verify / restore** CLI | `scripts/backup_restore_db.py` |
| 7 | Delivery is **at-least-once**, never exactly-once | (contract, unchanged) |

## 1. Structured JSON logging and correlation

- Python ``logging`` is configured once at process entry
  (`configure_logging()` in `src/core/logging_config.py`, stdlib-only). It
  attaches a single root `StreamHandler` whose `RedactingJsonFormatter`
  emits one-line JSON per record (`timestamp`, `level`, `logger`,
  `correlation_id`, `message`). It is idempotent and leaves pytest's
  `caplog` handler untouched, so existing message-level log assertions
  keep passing.
- The correlation id lives in a `ContextVar`
  (`CORRELATION_CONTEXT`). Three places set it:
  1. the FastAPI `CorrelationMiddleware` assigns a request id
     (`X-Request-ID` or a fresh UUID) and echoes it on the response;
  2. the outbox `event_id` is the durable correlation key — the API
     dispatch log, the publisher log (already `event_id=…`) and the
     worker all carry it;
  3. a Celery `task_prerun` / `task_postrun` signal pair
     (`src/core/celery_app.py`) copies `self.request.id` (which the
     publisher sets to `event_id`, ADR 0011 §7) into the ContextVar for
     the duration of the task.
- Acceptance: one `correlation_id` (= the `event_id`) retrieves the whole
  API → outbox → worker chain from the logs. The handlers are
  **module-level functions** so Celery's weakref-backed signal receivers
  are not garbage-collected.

## 2. Sensitive-field redaction

`redact()` runs on every emitted log message and scrubs:

- the configured secret values (`SECRET_KEY`, `MEDIA_SIGNING_KEY`,
  `PROVIDER_KEY_ENCRYPTION_KEY`, `MCP_TOKEN`, `DEFAULT_ADMIN_PASSWORD`
  and the `DATABASE_URL` / `REDIS_URL` connection strings);
- `Authorization ` / JSON `api_key` / `token` / `password` values;
- absolute paths rooted at `VIDEO_ROOT_PATH` / `PLAYBACK_CACHE_ROOT`
  (masked to `root/**`).

The analyzer's failure path still logs the *user prompt* and the *raw LLM
response* as a pre-existing, test-locked diagnostic. That is deliberate:
it is not a credential or a filesystem path, and removing it would break
an existing regression test. Redaction is therefore scoped to secrets and
paths — the genuinely dangerous data — and an acceptance test scans
structured logs for those markers.

## 3. At-least-once, not exactly-once

This ADR explicitly records the delivery semantics already frozen in ADR
0011: **at-least-once**. Duplicate publishes are possible (a lease may
expire between `send_task` and `mark_published`), and the consumer-side
`bind_or_create_running_task_log` idempotency short-circuit is the
defence. Nothing in Todo 24 changes those semantics; the docs repeat them
so operators do not assume exactly-once.

## 4. Health, metrics and the migration advisory lock

- `/livez` — pure process liveness (Todo 23).
- `/readyz` — DB `SELECT 1` + Redis PING + schema-at-expected-head
  (Todo 23).
- `/metrics` (new) — JSON gauges from `src/db/metrics.py`:
  - `outbox` — oldest pending seconds (lag), pending/publishing/failed
    counts;
  - `task_recovery` — cumulative `leases_recovered_total`
    (`recovery_attempt > 0`) plus the last heartbeat snapshot
    (`leases_recovered` = `timed_out`, `unleased_recovered` =
    `pending_recovered`), persisted to `AppRuntimeState` under
    `heartbeat_last_counters` by the heartbeat task;
  - `analysis_checkpoint` — per running analysis, completed vs total
    sub-chunks (success vs allocated `SessionAnalysisCheckpoint` rows).
- The alembic migration advisory lock (`pg_advisory_lock`, Todo 23)
  serializes container bootstrap; worker / beat / publisher healthchecks
  and CPU / memory / log-rotation limits are docker-compose-level (Todo
  23).

## 5. Backup / verify / restore workflow

`scripts/backup_restore_db.py` provides three subcommands:

- `backup --database-url <url> [--schema <name>] --output <path>` —
  `pg_dump` custom-format plus a `sha256` sidecar;
- `verify --output <path>` — recompute and compare the checksum;
- `restore --database-url <url> [--schema <name>] --dump <path>` —
  `pg_restore --exit-on-error` (a partial restore aborts).

Operators must run the **verified** backup before the irreversible
migrations listed in AGENTS.md §10 / the README (Provider-key
encryption, timestamp UTC rewrite, FK delete-policy changes). The
round-trip is proven by a PostgreSQL integration test that dumps a
migrated schema and restores it into a THROWAWAY temp database,
asserting the Alembic revision and key row counts match. `pg_dump` /
`pg_restore` must match the server major version; passwords are passed
via `PGPASSWORD` (never on the command line) and `str(URL)` password
masking is deliberately avoided.

## Status

Accepted. Supersedes the scattered "Todo 24" operations pointers in ADR
0011 and earlier evidence manifests.
