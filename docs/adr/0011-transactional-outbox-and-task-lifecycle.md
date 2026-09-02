# ADR 0011 — Transactional outbox and task lifecycle contract

- Status: accepted
- Date: 2026-09-02
- Owner: architecture-consolidation Wave 3 / Todo 11
- Related: plan `.omo/plans/architecture-consolidation.md` (Wave 3, Todo 11),
  ARCHITECTURE.md §5.1–§5.4, AGENTS.md §5–§7, ADR 0009 (raw_mp4-only).
- Successors: Todo 12 (persistence), Todo 13 (publisher), Todo 14 (cutover).

## Context

The current dispatcher path — `src/infrastructure/tasks/celery_dispatcher.py`
→ `src/services/task_dispatch_control.py` → `celery_app.send_task` — couples
the **business transaction** that mutates domain state (creating / superseding
`TaskLog`, sealing sessions, etc.) to the **broker side effect** of pushing
a Celery message. The `TaskLog` table tries to do double duty:

1. it owns the *business* lifecycle of one logical run (PENDING → RUNNING →
   terminal, dedupe, cancel, lease, recovery, supervisor heartbeats);
2. it owns the *envelope* that reaches the broker — the
   `queue_task_id` column carries the Celery `task.id` written **after**
   the broker round-trip returns, so a crash between commit and
   `send_task` (or between `send_task` and commit) leaves the system in
   an observable inconsistency: the run is "running" but no message ever
   made it to a worker, or the message was published but the row was
   never updated.

This is the classic dual-write problem, and the project has already
absorbed its failure modes in two specific places:

- `task_dispatch_control.create_pending_task_log` plus the `send_task`
  branch in `CeleryTaskDispatcher._enqueue_with_dedupe` open their own
  `SessionLocal`, which means the `TaskLog` insert and the broker push
  are not in the **same database transaction** as the business mutation
  that triggered them. Sealing a `video_session` and dispatching the
  analyzer are two writes a Redis blip can tear apart.
- `record_deferred_hot_scan` / `supersede_active_hot_scan` add ad-hoc
  compensating logic on top of that split-brain state, instead of being
  able to express "the business committed but the broker hasn't
  published yet" as a first-class row in the database.

The architecture-consolidation plan (Wave 3) replaces the dual write
with a PostgreSQL transactional outbox:

- business code commits `TaskLog` and a paired `outbox_event` row in one
  transaction;
- an **independent** PostgreSQL-only publisher polls `outbox_event`,
  pushes to the broker, and marks the row `published`;
- consumers receive the broker message with a fixed `task_id` (the
  `outbox_event.event_id`), and use that as the consumer-side
  idempotency key so duplicate publishes never produce duplicate
  business side effects.

Todo 11 **only** freezes the contract: the table field list, the
state machine, the retry parameters, the payload rules, and the split
of responsibilities between `TaskLog` and `outbox_event`. The actual
SQLAlchemy model, the Alembic migration, and the publisher process are
Todo 12 / 13. This ADR is the contract those three (and Todo 14, the
cutover) read against.

## Decision

### 1. Two tables, two responsibilities

The plan is explicit: `TaskLog` is **not** replaced by `outbox_event`.
Both tables exist after Todo 14 and serve distinct purposes.

| Concern | `task_log` (existing, unchanged here) | `outbox_event` (new, designed here) |
| --- | --- | --- |
| Owns | one **business run**'s lifecycle | one **broker message**'s publication |
| States | `pending / running / success / skipped / failed / timeout / cancelled` | `pending / publishing / published / failed` |
| Identity | `task_log.id` (BIGSERIAL) | `outbox_event.event_id` (UUID) |
| Active dedupe | yes — partial unique on `(dedupe_key) WHERE status IN (pending, running)` | no — global unique on `event_id`, plus 1:1 link to `task_log_id` |
| Cancel / lease / recovery | yes — `cancel_requested`, `lease_owner`, `lease_expires_at`, `recovery_attempt` | lease only for **publisher** ownership; cancel is propagated by marking the run terminal, not by editing the outbox row |
| Audit / observability | `task_log.message`, `task_log.detail_json`, `TaskTransitionLog` (Todo 17) | `attempt_count`, `last_error`, `published_at` |
| Cleanup | 7 days (existing behavior, kept) | `published` rows: 30 days; `failed` rows: retained until manual intervention |
| Mutated by | application use cases, Celery workers, maintenance tasks | application use cases (`INSERT … ON CONFLICT DO NOTHING`) and the publisher (`UPDATE … WHERE claimed_by = …`) |
| Read by | API, dashboard, maintenance, supervisor | publisher only (writers) + supervisor (lag/degraded metrics) |

The **1:1 link** between a run and its outbox row is the seam: every
business dispatch writes exactly one `outbox_event`; every published
message originates from exactly one row; and `event_id` is reused as
the Celery `task_id` so a worker can detect "I already processed this
run" without consulting the broker.

### 2. Outbox table field contract

The persistence model (Todo 12) MUST use exactly the columns and
constraints below. Field names are SQLAlchemy-agnostic so the contract
test can import them without standing up a database, but the migration
target is PostgreSQL 12+ (the project's supported version).

| Field | Type (PostgreSQL) | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| `id` | `BIGSERIAL` | NO | sequence | surrogate PK; opaque to callers |
| `event_id` | `UUID` | NO | — | global unique; reused as Celery `task_id` and consumer idempotency key |
| `task_log_id` | `BIGINT` | NO | — | 1:1 FK → `task_log(id)`; UNIQUE constraint enforces "one outbox row per run" |
| `dedupe_key` | `TEXT` | YES | `NULL` | mirror of `task_log.dedupe_key` for audit; non-unique; nullable so the partial unique index below can match the `WHERE task_log_id IS NOT NULL` predicate consistently |
| `task_name` | `TEXT` | NO | — | Celery task dotted path; must be in the registry whitelist at emit time |
| `queue` | `TEXT` | NO | — | Celery queue name; defaults come from `OutboxCommandRegistry.queue_for(task_name)` |
| `args_json` | `JSONB` | NO | — | JSON-safe list; `json.dumps()` round-trip MUST hold |
| `kwargs_json` | `JSONB` | NO | — | JSON-safe dict; same round-trip requirement |
| `status` | `TEXT` | NO | — | CHECK constraint `IN ('pending','publishing','published','failed')` |
| `attempt_count` | `INT` | NO | `0` | monotonic; incremented by publisher |
| `next_attempt_at` | `TIMESTAMPTZ` | NO | — | publisher schedules next try; initial = `created_at` |
| `claimed_by` | `TEXT` | YES | `NULL` | publisher instance id while in `publishing` |
| `lease_expires_at` | `TIMESTAMPTZ` | YES | `NULL` | publisher lease; default 60s |
| `published_at` | `TIMESTAMPTZ` | YES | `NULL` | wall-clock success; set in the same transaction as `status='published'` |
| `last_error` | `TEXT` | YES | `NULL` | truncated publisher error for operator inspection |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | row creation |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | bumped on every UPDATE |

#### Index / constraint plan

1. `PRIMARY KEY (id)` — surrogate.
2. `UNIQUE (event_id)` — global idempotency. Re-publishing the same
   event_id is impossible by construction; the partial unique index
   below guarantees the *creation* race.
3. `UNIQUE (task_log_id)` — "one outbox row per business run". Inserts
   that violate this MUST fail with the existing
   `postgresql_insert(...).on_conflict_do_nothing()` pattern that
   `task_dispatch_control.create_pending_task_log` already uses.
4. **Partial unique index** on `(task_log_id)` `WHERE status = 'pending' AND task_log_id IS NOT NULL`
   — this is the concurrency safety net. Two concurrent writers cannot
   both observe `TaskLog` is fresh and create two `pending` outbox rows
   for the same run; the second `INSERT … ON CONFLICT DO NOTHING`
   returns no row and the caller falls back to "row already exists,
   reuse the published message". This index is intentionally stricter
   than the global `task_log_id` UNIQUE because it lets a finished
   outbox row be replaced after a manual retry without a `TRUNCATE` or
   constraint drop.
5. `INDEX (status, next_attempt_at)` — publisher hot path: "next batch
   of pending rows whose retry timer has elapsed". Partial:
   `WHERE status = 'pending'` so the index stays small even after the
   table fills with historical `published` rows.
6. `INDEX (status, updated_at)` — supervisor / degraded metric:
   "oldest pending or publishing row age". Partial:
   `WHERE status IN ('pending','publishing')`.
7. `INDEX (claimed_by)` partial `WHERE status = 'publishing'` —
   publisher instance liveness; not used in the hot path.
8. `FOREIGN KEY (task_log_id) REFERENCES task_log(id) ON DELETE RESTRICT`
   — the outbox MUST NOT outlive its run silently. RESTRICT forces
   cleanup operators to drain the outbox first; this is intentional
   (we never want a "ghost" message with no run to back it up).

> **No** row-level security, **no** triggers, **no** partitioned tables
> in this ADR. Partitioning by `created_at` is a candidate optimization
> for a future wave if the `published` retention is extended beyond 30
> days.

### 3. State machine

```
                        +--------------+
                        |  (created)   |
                        +------+-------+
                               | INSERT (atomic with TaskLog)
                               v
                        +------+-------+
       +---------------->|   pending    |<-----------------+
       |                +------+-------+                  |
       |                       |                          | manual retry (mgmt API)
       |              publisher claim                     | from 'failed' only
       |              (FOR UPDATE SKIP LOCKED,             |
       |               set publishing + lease)            |
       |                       v                          |
       |                +------+-------+                  |
       +----------------|  publishing  |------------------+
       | retryable      +------+-------+   retryable      |
       | broker error          |          lease expired    |
       | (≤ N attempts)        |                          |
       |                       | send_task(task_id=event_id) succeeded
       |                       v                          |
       |                +------+-------+                  |
       |                |  published   |  (terminal)      |
       |                +--------------+                  |
       |                                                  |
       |    send_task failed / lease lost / crash         |
       +-------------------------------------------------+
                        |
                        v (after attempt_count = N)
                 +------+-------+
                 |   failed     |  (terminal, manual only)
                 +--------------+
```

**Allowed transitions (the contract):**

| From | To | Trigger | Pre-conditions |
| --- | --- | --- | --- |
| (none) | `pending` | `INSERT INTO outbox_event …` in same tx as `task_log` INSERT | task_name in registry; `args_json` / `kwargs_json` JSON-safe; `task_log_id` not already pending (partial unique) |
| `pending` | `publishing` | publisher `UPDATE … SET status='publishing', claimed_by=$1, lease_expires_at=now()+60s WHERE id=$2 AND status='pending'` | row is currently `pending`; publisher is allowed to claim (CAS by `status`) |
| `publishing` | `published` | publisher `UPDATE … SET status='published', published_at=now(), claimed_by=NULL, lease_expires_at=NULL WHERE id=$1 AND status='publishing' AND claimed_by=$2` | broker call returned without exception; `task_id` set to `event_id` |
| `publishing` | `pending` | publisher rollback / crash recovery; publisher `UPDATE … SET status='pending', claimed_by=NULL, attempt_count=attempt_count+1, next_attempt_at=now()+backoff WHERE id=$1 AND status='publishing' AND claimed_by=$2` | retryable broker error OR lease lost; `attempt_count < N` |
| `publishing` | `failed` | publisher `UPDATE … SET status='failed', claimed_by=NULL, last_error=$1 WHERE id=$2 AND status='publishing' AND claimed_by=$3` | `attempt_count >= N` after the retryable error path; non-retryable error (e.g. validation refused at the broker) |
| `pending` | `failed` | publisher `UPDATE … SET status='failed' …` | `attempt_count >= N` and a final attempt failed |
| `failed` | `pending` | mgmt API / CLI only; **never** the publisher loop | operator-initiated `POST /api/v1/outbox/{id}/retry` (out of scope for Todo 11 — defined here so the contract knows it is the only legal reversal) |
| `published` | (anything) | **forbidden** | terminal |

**Forbidden transitions** (any of these MUST raise
`OutboxStateError` in the contract layer):

- `pending → published` (skipping `publishing`).
- `published → anything`.
- `failed → publishing` (operators reset to `pending` first).
- `* → pending` from anything other than `publishing` (retry) or
  `failed` (operator) — i.e. no `pending → pending`, no
  `pending → publishing` from a non-pending state, etc.

### 4. Retry / lease parameters (defaults, override at mgmt API)

These are the **plan defaults** (`.omo/plans/architecture-consolidation.md`,
"Outbox 默认参数"). They are constants the publisher imports; the
contract layer only enforces that the parameters exist.

| Parameter | Default | Notes |
| --- | --- | --- |
| Publisher poll interval | 5 s | publisher loop sleep between empty polls |
| Batch size | 100 | `LIMIT 100` per claim; one transaction per batch |
| Lease duration | 60 s | `lease_expires_at = now() + 60s`; reclaimed when `now() > lease_expires_at` |
| Backoff base | 5 s | `next_attempt_at = now() + min(300, 5 * 2^(attempt_count-1))` |
| Backoff cap | 300 s | 5 min; no row waits longer than 5 min between attempts under continuous failure |
| Max attempts | 12 | `attempt_count >= 12` ⇒ terminal `failed` |
| Published retention | 30 days | hard delete; vacuum-friendly |
| Failed retention | indefinite | only manual `DELETE` by operator; no automatic cleanup |
| Backlog degraded threshold | oldest `pending` or `publishing` `updated_at` > 300 s | emits a metric; the publisher does **not** delete or skip |

Backoff math is `min(backoff_cap, backoff_base * 2 ** (attempt_count - 1))`
starting at `attempt_count=1` (so attempt 1 schedules +5s, attempt 2 +10s,
…, attempt 9 +2560s clamped to +300s, attempts 10–12 stay at +300s).

### 5. Payload contract (JSON-safe, no PII, no media, no secrets)

The outbox is **not** a content store. The rules:

- `args_json` and `kwargs_json` MUST round-trip through `json.dumps()`
  and `json.loads()` with the original Python types preserved within the
  whitelist below.
- Allowed value types: `None`, `bool`, `int`, `float`, `str`, `list`,
  `tuple` (serialized as `list`), `dict` (string keys only), `UUID`
  (serialized as canonical hex string), `datetime` / `date`
  (ISO 8601 UTC, with timezone).
- Rejected at the contract layer (raises `OutboxPayloadError`):
  - raw `bytes`, `bytearray`, `memoryview` (and any other
    `Buffer`/`BinaryIO` instance);
  - arbitrary Python objects without a registered encoder
    (`object` not in the allow-list above);
  - `set` / `frozenset` (unstable iteration order is a foot-gun for
    replay);
  - dict with non-string keys;
  - deeply nested structures exceeding the configured cap (default
    `MAX_PAYLOAD_DEPTH = 8`) or exceeding
    `MAX_PAYLOAD_BYTES = 32 KiB` after serialization.
- **Hard no** for sensitive / large content, enforced by code review
  and (Todo 14) by linting at the call site:
  - API keys, tokens, JWTs, session cookies;
  - `MEDIA_SIGNING_KEY`, `SECRET_KEY`, `PROVIDER_KEY_ENCRYPTION_KEY`
    values;
  - raw video bytes, decoded frame buffers, base64 video URLs;
  - file system paths inside `VIDEO_ROOT_PATH` (use opaque IDs and let
    the worker resolve them);
  - PII (names, addresses, phone numbers); the worker reads them from
    the database by id.
- `dedupe_key` is plain text and mirrors `task_log.dedupe_key`; it is
  not unique at the outbox level. The partial unique index on
  `(task_log_id) WHERE status = 'pending' AND task_log_id IS NOT NULL`
  is the structural guarantee that prevents duplicate enqueue.

### 6. Registry (task name whitelist)

```
src.application.outbox.registry.OutboxCommandRegistry
```

- `register(task_name, queue)` — class-level registration; the
  decorator `@OutboxCommandRegistry.bind("src.tasks.x.x_task",
  queue="default")` is the convenience form.
- `is_allowed(task_name) -> bool` — checked at emit time; unknown names
  raise `OutboxContractViolation`.
- `queue_for(task_name) -> str` — `is_allowed` MUST be true first;
  raises the same exception otherwise.
- The registry starts populated with the four Celery task names that
  `CeleryTaskDispatcher` already targets (so callers do not have to
  register anything in the cutover PR):
  - `src.tasks.session_build.full_build_task` (queue: default)
  - `src.tasks.session_build.hot_build_task` (queue: default)
  - `src.tasks.analyzer.analyze_session_task` (queue: `analysis_hot` /
    `analysis_full` — the registry entry carries a queue template;
    callers override at emit time if needed)
  - `src.tasks.summarizer.generate_daily_summary_task` (queue: default)
  - `src.tasks.webhook.send_webhook_task` (queue: default)
- The registry is process-local. It is **not** persisted; it is the
  static declaration of "what the application is willing to publish".
  A `task_name` present at runtime but absent from the registry is a
  contract violation, not a configuration knob.

### 7. Celery task ID is `event_id`

The publisher MUST set `celery_app.send_task(name, task_id=event_id, …)`
when it pushes a message. The consumer-side rule that goes with this is
already in the project's dispatch contract and survives the cutover
intact:

- when a worker receives a message, it calls
  `bind_or_create_running_task_log(queue_task_id=task_id, …)` (existing
  helper, kept verbatim);
- if the resulting `TaskLog` row is **terminal** (one of
  `success / skipped / failed / timeout / cancelled`), the worker
  returns `{"skipped": True, "reason": "stale_message"}` and exits
  without re-running the business side effects — this is the
  `stale-message` short-circuit called out in `TaskLog` baseline and
  AGENTS.md §5.1.
- business writes performed by the worker use
  `INSERT … ON CONFLICT (event_id) DO NOTHING` (Todo 18 / 19 will
  refactor the existing checkpoints and summaries to match) so a
  re-delivery during the publish/success race never creates two
  `EventRecord` / `DailySummary` rows.

### 8. Failure matrix

| Failure | Detection | Outcome |
| --- | --- | --- |
| Redis broker unreachable | publisher `send_task` raises `OperationalError` / `ConnectionError` | row stays `pending`, `next_attempt_at` bumped per backoff; business commit is unaffected (it never touched Redis) |
| Publisher crashes mid-claim | publisher lease not refreshed within 60 s | another publisher observes `lease_expires_at < now()`, claims the row, repeats the publish |
| Publisher publishes, then crashes before `UPDATE … status='published'` | row is still `publishing` until lease expires | lease expires → another publisher re-publishes; consumer sees the same `task_id` twice; second delivery hits the stale-message short-circuit because the first delivery already marked the `TaskLog` terminal |
| Broker accepts but worker never starts | publisher cannot tell apart "delivered" from "lost in transit"; we treat any non-exception return as `published` | acceptable: at-least-once; duplicate publish path is covered above |
| Broker rejects the message (e.g. unknown task) | `send_task` raises `NotRegistered` | publisher marks row `failed` immediately (`attempt_count >= N` is not required for non-retryable errors) |
| TaskLog row deleted manually | `outbox_event.task_log_id` FK with `ON DELETE RESTRICT` raises on `DELETE FROM task_log …` | operator sees a clear FK error and uses the cleanup query to delete the outbox row first (Todo 24 runbook) |
| Two business transactions create the same `task_log_id` row concurrently | partial unique index on `(task_log_id) WHERE status='pending'` | second insert's `ON CONFLICT DO NOTHING` returns no row; caller treats the first row as the one to publish |
| Worker processes the message twice (broker re-delivery, prefetch) | `task_id = event_id`; consumer's `bind_or_create_running_task_log` sees terminal row | second delivery short-circuits with `skipped: True`; business side effects run exactly once |
| Manual retry of a `failed` row | mgmt API `POST /api/v1/outbox/{id}/retry` (out of scope here, listed for completeness) | row moves `failed → pending`, `attempt_count = 0`, `next_attempt_at = now()`; publisher picks it up next batch |
| `published` row never cleaned | retention job (out of scope here, but documented) | `DELETE FROM outbox_event WHERE status='published' AND published_at < now() - INTERVAL '30 days'`; vacuum-friendly |
| Backlog grows > 300 s | supervisor metric `outbox_oldest_pending_seconds > 300` | logs / dashboard degraded flag; the publisher does not skip rows |

### 9. Retention

| Table / status | Retention | Source of truth |
| --- | --- | --- |
| `task_log` | 7 days (existing behavior) | `src/tasks/task_maintenance.py:_cleanup_old_task_logs` (unchanged) |
| `outbox_event` `published` | 30 days | Todo 13 publisher, on its own clock |
| `outbox_event` `failed` | until manual `DELETE` | mgmt API + runbook; never auto-purged |
| `outbox_event` `pending` / `publishing` | until terminal | publisher drives state; never auto-purged |
| `pipeline_transition_log` (separate Todo 17) | independent of TaskLog cleanup; preserved after TaskLog 7-day purge | future |

The 7-day TaskLog retention is intentionally **shorter** than the
outbox retention because:

- the outbox row still references `task_log_id` but the FK is
  `ON DELETE RESTRICT`, so a TaskLog cleanup job that runs against a
  non-terminal outbox row will fail loudly rather than silently break
  the publish path;
- the outbox `published` row is the **only** durable record of "this
  command was delivered" once the TaskLog row has been purged; we keep
  it for an additional 23 days so post-incident investigations have
  time to query broker-side anomalies.

### 10. Adversarial / misuse guardrails

The contract layer MUST refuse these at emit time (raising
`OutboxContractViolation` or the appropriate subclass):

1. **Non-JSON-safe payload** — see §5.
2. **Unknown task_name** — `OutboxCommandRegistry.is_allowed(...) == False`.
3. **Duplicate `event_id`** at the contract layer — `OutboxEvent` is a
   frozen dataclass and the in-memory helper `register_emitted_event_id`
   keeps a process-local set; this is **not** the database guarantee
   (the DB unique constraint is the source of truth), it is a fast
   early-reject to catch obvious caller bugs in tests. The
   in-memory set is intentionally per-process; restart clears it.
4. **Manual reset from `published`** — `transition()` raises
   `OutboxStateError`; the only way out is a SQL
   `UPDATE … SET status='failed'` followed by a fresh emit (the
   application never does this; it is reserved for the recovery
   runbook).
5. **Direct publisher bypass** — Todo 14 removes the
   `CeleryTaskDispatcher` `send_task` path entirely; until then, the
   contract layer cannot prevent a caller from going around it, but
   the dispatcher code that still does `send_task` is marked
   "decommissioned — see Todo 14" in its docstring.

## Consequences

Positive

- Business transactions and outbox writes share one database transaction.
  A sealed session that fails to dispatch now leaves zero rows behind
  instead of an orphaned `TaskLog` row whose message never reached the
  broker.
- The publisher is independent of Celery Beat. It is a separate
  container (Todo 13) that talks only to PostgreSQL and the broker.
  Heartbeat / Celery beat failures do not stop publishes.
- Consumer idempotency is end-to-end: `event_id` is the broker `task_id`
  and the `TaskLog.bind_or_create_running_task_log` short-circuit plus
  `INSERT … ON CONFLICT (event_id) DO NOTHING` together ensure duplicate
  publishes never produce duplicate business side effects.
- The retry/lease parameters are constants in the contract module and
  can be overridden by a single mgmt-API hook without code changes
  (Todo 13 implementation detail).
- The TaskLog table is untouched. The existing 7-day cleanup, the
  existing lease / cancel / recovery behavior, the existing
  characterization tests in `tests/unit/test_task_dispatch_binding.py`
  and `tests/unit/test_task_maintenance.py` all survive.
- Architecture test stays at zero exceptions: the new module lives
  under `src.application.outbox.*`, which neither creates a new layer
  boundary nor breaks any of the existing ones (`src.api`,
  `src.mcp`, `src.tasks` are still forbidden from importing
  `src.infrastructure`).

Costs / follow-ups

- Two tables for what used to be one. Operators used to grepping
  `task_log.queue_task_id` for broker-side anomalies now need to join
  `task_log` ↔ `outbox_event` on `task_log.id = outbox_event.task_log_id`.
  Todo 24 ships a runbook query and a dashboard card.
- The `event_id` ↔ `task_id` invariant is now load-bearing. The
  contract module exports a single helper
  `assert_event_id_is_task_id(event_id, task_id)` and the consumer
  side raises if they ever diverge. Any future "let Celery auto-assign
  task_id" change is a contract violation, not a refactor.
- The partial unique index is non-trivial SQL. Migration Todo 12 must
  ship the index in the same revision as the table (PostgreSQL handles
  it natively, no extra round trip needed).
- The 30-day retention for `published` rows creates a soft requirement
  on `pg_repack` / `VACUUM` cadence. Out of scope for this ADR, but the
  operations runbook (Todo 24) MUST call it out.
- A failed row cannot be reset by the publisher loop. The mgmt API for
  manual retry is intentionally **not** part of this ADR; it is
  reserved for a follow-up so the contract freezes first.

## Field contract (canonical list)

The persistence model (Todo 12) MUST define the columns exactly as
listed in §2 above. Field names are stable and cannot be renamed
without an ADR amendment. Allowed value types per §5 cannot be relaxed
without an ADR amendment. The `event_id` ↔ `task_id` invariant in §7
cannot be removed.

The contract layer (`src/application/outbox/contracts.py`,
`payload_validator.py`, `state_machine.py`, `registry.py`, `errors.py`)
exists precisely so the migration and the publisher can import these
rules without standing up a database.

## State machine summary

(See §3 table.) The four states are `pending`, `publishing`, `published`,
`failed`. Only `pending` and `publishing` are non-terminal. The only
way out of `failed` is a manual mgmt API call (out of scope for Todo 11;
listed in the failure matrix for completeness). The only way out of
`published` is the 30-day retention job.

## Retry parameters

(See §4.) Defaults: poll 5 s, batch 100, lease 60 s, backoff base 5 s,
backoff cap 300 s, max attempts 12, `published` retention 30 days,
backlog degraded 300 s.

## Retention

(See §9.) `task_log` 7 days (unchanged). `outbox_event` `published` 30
days. `outbox_event` `failed` retained indefinitely. `outbox_event`
non-terminal retained until terminal.

## Failure matrix

(See §8.) Redis down, publisher crash, duplicate publish, duplicate
broker message, manual retry, FK orphan, concurrent enqueue, stale
worker, non-retryable broker rejection, retention cleanup, backlog
growth — all explicitly covered.

## Idempotency rules

(See §7.) `task_id = event_id`. Worker bind short-circuits terminal
runs with `{"skipped": True, "reason": "stale_message"}` (existing
behavior, kept verbatim). Business writes use
`INSERT … ON CONFLICT (event_id) DO NOTHING` or equivalent CAS.

## Migration risk

The migration (Todo 12) adds one table and three indexes. There is **no**
data backfill: no historical TaskLog row is automatically promoted to an
outbox row. The cutover (Todo 14) happens **after** a quiet period that
the operator defines, because pending / running dispatches from before
the migration will be published through the existing
`CeleryTaskDispatcher.send_task` path until they terminate, and only
**new** dispatches go through the outbox. The migration is therefore
**forward-only** and **non-destructive** of existing behavior.

The migration is irreversible in the same sense as the prior
`20260825_0012`, `20260825_0014`, and `20260826_0017` revisions: a
`downgrade` would drop the partial unique index that other code paths
rely on (Todo 14 onwards), so the migration will explicitly raise on
`downgrade()`. Operators wanting to roll back MUST restore the
pre-upgrade database backup that AGENTS.md §10 already requires for
irreversible migrations.

## Index plan

(See §2, "Index / constraint plan".) Eight objects total: PK, global
unique on `event_id`, unique on `task_log_id`, partial unique on
`task_log_id WHERE status='pending'`, partial index on
`(status, next_attempt_at) WHERE status='pending'`, partial index on
`(status, updated_at) WHERE status IN ('pending','publishing')`,
partial index on `(claimed_by) WHERE status='publishing'`, and the FK
constraint. No other indexes; the supervisor / dashboard queries use
the partial ones.

## Outbox / TaskLog responsibilities split

(See §1.) Repeated here so this ADR is grep-able: `task_log` owns the
business run lifecycle; `outbox_event` owns broker publication. They
are linked 1:1 by `outbox_event.task_log_id` and `event_id` is the
broker `task_id` and the consumer idempotency key. **Do not** replace
`task_log` with `outbox_event`; the plan explicitly requires both.

## Open questions

None. The plan section "Outbox 默认参数" and "TaskLog 继续负责" define
the contract space fully; the cutover (Todo 14) and the operations
runbook (Todo 24) will surface concrete operational questions later,
but the **contract** itself is closed.

## Verification

Contract-level (this Todo 11):

- `tests/unit/test_outbox_payload.py` — accepts JSON-safe types,
  rejects `bytes` / `bytearray` / `set` / arbitrary objects / oversize
  payloads / non-string dict keys.
- `tests/unit/test_outbox_state_machine.py` — every legal transition
  succeeds; every illegal transition raises `OutboxStateError`.
- `tests/unit/test_outbox_contract.py` — DTO round-trip
  (`to_row_dict` ↔ `from_row_dict`), unknown `task_name` rejection,
  duplicate `event_id` rejection at the contract layer, registry
  whitelist semantics.

Module-level:

```bash
python3 -c "from src.application.outbox.contracts import OutboxEvent; \
            from src.application.outbox.state_machine import OutboxState, transition; \
            from src.application.outbox.payload_validator import ensure_json_safe; \
            from src.application.outbox.registry import OutboxCommandRegistry; \
            from src.application.outbox.errors import OutboxStateError; \
            print('imports OK')"
python3 -m pytest tests/unit -q
ruff check .
ruff format --check src tests
mypy src
python3 -m pytest tests/architecture -q
```

Migration-level (Todo 12) and end-to-end (Todo 13/14) are out of scope
for this ADR.

## Reversibility

The contract layer is reversible: deleting
`src/application/outbox/` and the ADR removes every artifact this Todo
introduces. The downstream migrations and publisher are separate
commits and have their own reversibility story (migration is
forward-only by design; publisher is a process that can be turned off
without affecting the application code path until Todo 14).

## Status

accepted — supersedes the implicit "the broker publish is part of
`CeleryTaskDispatcher`" assumption used by the codebase before this
ADR. Downstream tasks (12, 13, 14, 17) MUST consume the field list,
state machine, retry parameters, payload rules and registry semantics
above without amendment.
