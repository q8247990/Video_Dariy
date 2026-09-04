> **中文版**: [ARCHITECTURE.md](ARCHITECTURE.md)

# Architecture Document

## 1. Purpose

This document describes the system architecture, module responsibilities, core business workflows, key data models, and deployment topology based on the current codebase. It serves as a reference for maintenance, delivery, and further development.

## 2. System Overview

This is an offline analysis system for home surveillance video.

Input consists of surveillance recording files from a NAS or local directory. Output includes structured events, playable sessions, daily family digests, and query capabilities for the frontend, bots, and agents.

The project is not a real-time stream processing system. It is an offline pipeline built around "directory scanning + asynchronous analysis".

## 3. High-Level Architecture

```text
                +----------------------+
                |   React + Nginx UI   |
                +----------+-----------+
                           |
                           v
                +----------------------+
                |   FastAPI Backend     |
                |  REST API / Media /   |
                |       MCP Server      |
                +----+-------------+----+
                     |             |
                     |             v
                     |     +---------------+
                     |     | OpenAI Compat  |
                     |     | LLM Providers  |
                     |     +---------------+
                     |
                     v
             +-------------------+
             | PostgreSQL        |
             | models + logs     |
             +-------------------+
                     ^
                     |
             +-------------------+
             | Celery Worker     |
             | build/analyze/    |
             | summary/webhook   |
             +-------------------+
                     ^
                     |
             +-------------------+
             | Redis             |
             | broker/backend    |
             +-------------------+

Video Directory / NAS -> Scan Task -> VideoFile -> VideoSession -> EventRecord -> DailySummary
```

## 4. Layers and Module Responsibilities

### 4.1 `src/main.py`

- Creates the FastAPI application
- Registers the API Router and MCP Router
- Runs `init_db()` at startup, which executes Alembic migrations and initializes the default admin user
- Exposes `/health`, `/livez`, `/readyz`, `/metrics`, and `/health/bootstrap`

### 4.2 `src/api/`

Responsibility: External HTTP interface layer.

Key files:

- `src/api/v1/api.py`
- `src/api/v1/endpoints/auth.py`
- `src/api/v1/endpoints/video_sources.py`
- `src/api/v1/endpoints/tasks.py`
- `src/api/v1/endpoints/events.py`
- `src/api/v1/endpoints/sessions.py`
- `src/api/v1/endpoints/daily_summaries.py`
- `src/api/v1/endpoints/chat.py`
- `src/api/v1/endpoints/llm_providers.py`
- `src/api/v1/endpoints/webhooks.py`
- `src/api/v1/endpoints/media.py`
- `src/api/v1/endpoints/home_profile.py`
- `src/api/v1/endpoints/system_config.py`
- `src/api/v1/endpoints/onboarding.py`

Interface characteristics:

- Unified JWT authentication; dependencies defined in `src/api/deps.py`
- Provides paginated lists, detail views, create/update operations, and task triggers
- `media.py` serves raw file streams, session merged streams, and HLS playback manifests

### 4.3 `src/application/`

Responsibility: Application orchestration layer.

Sub-modules:

- `application/pipeline/`
  - Defines command objects and orchestrators
  - `PipelineOrchestrator` dispatches build, analysis, daily report, and webhook tasks
- `application/qa/`
  - Q&A use cases, retrieval strategies, evidence compression, output DTOs
- `application/mcp/`
  - Service wrappers for MCP tool invocations
- `application/prompt/`
  - Prompt compilation and context contract objects

Characteristics:

- This layer hosts "business orchestration logic" but is not a full DDD implementation; some core business rules still reside in `services`
- `src/application/bootstrap.py` currently serves as a compatibility placeholder and has not evolved into a true DI entry point

### 4.4 `src/services/`

Responsibility: Core domain services.

Key services:

- `session_build/`: Directory scanning, deduplication, session merging, and sealing split into discovery, dedupe, reducer, persistence, seal policy, and runner stages
- `analysis/`: Session analysis split into claim, chunk planning, sub-chunk execution, checkpoints, aggregation, and finalization
- `summarizer/`: Daily-summary scheduling, evidence, generation, parsing, and finalization stages
- `dispatch/` and `maintenance/`: Task claim/lease/dedupe/cancel plus hot scheduling, recovery, and retention policies
- `session_analysis_video.py`: Session slicing, video segment processing
- `daily_summary/*`: Daily report preprocessing and output parsing
- `home_profile.py`: Family profile context building
- `dashboard.py`: Dashboard statistics
- `provider_selector.py`: Vision model / QA model selection
- `video_source_validator.py`: Video directory validation
- `webhook_payload.py` / `webhook_subscription.py`: Webhook event payloads and subscription matching
- `task_dispatch_control.py`: Task deduplication, running-state protection, task log binding

This is the most important layer in the current project, housing the majority of real business rules.

### 4.5 `src/tasks/`

Responsibility: Celery async execution layer.

Key tasks:

- `session_build.py`
  - Hot scan, full scan
- `analyzer.py`
  - Session visual analysis
- `summarizer.py`
  - Daily family report generation and scheduled dispatch
- `webhook.py`
  - Async webhook delivery
- `task_maintenance.py`
  - Heartbeat, timeout recovery, log cleanup

Scheduling strategy:

- `heartbeat`: Runs every 60 seconds
- `dispatch_scheduled_daily_summary_task`: Runs every 60 seconds
- Analysis tasks enter `analysis_hot` / `analysis_full` queues by priority

### 4.6 `src/models/`

Responsibility: Database model layer.

Core entities:

- `AdminUser`: Admin account
- `VideoSource`: Video source configuration
- `VideoFile`: Scanned recording file
- `VideoSession`: Logical session formed by merging consecutive recording clips
- `VideoSessionFileRel`: Ordered relationship between sessions and file segments
- `EventRecord`: Structured event identified by AI
- `DailySummary`: Daily report
- `LLMProvider`: Model service configuration and capability flags
- `TaskLog`: Async task log and status
- `WebhookConfig`: Webhook configuration
- `HomeProfile` / `HomeEntityProfile`: Family profile
- `SystemConfig`: System-level configuration
- `AppRuntimeState`: Runtime protection state, e.g., daily report dispatch guard
- `ChatQueryLog`: Q&A query log
- `McpCallLog`: MCP call log

### 4.7 `src/db/`

Responsibility: Database infrastructure.

- `session.py`: Engine / SessionLocal / get_db
- `init_db.py`: Alembic upgrade, default admin initialization, database availability retry,
  migration advisory lock
- `readiness.py`: health-check implementation for `/livez` / `/readyz`
- `metrics.py`: `/metrics` observability gauges (outbox lag/failures, task recovery,
  checkpoint progress)
- `base.py`: Model registration

Migration conventions:

- `20260320_0001` is the current PostgreSQL baseline snapshot
- Subsequent schema changes evolve through incremental Alembic revisions; runtime `create_all()` is no longer used as a migration mechanism

### 4.8 `src/providers/` and `src/infrastructure/`

Responsibility: External system adapters.

- `providers/openai_client.py`: OpenAI-compatible API client
- `infrastructure/llm/openai_gateway.py`: LLM Gateway factory and adapter
- `infrastructure/tasks/celery_dispatcher.py`: Task dispatcher implementation

### 4.9 `src/mcp/`

Responsibility: MCP Server and tool capabilities.

Key capabilities:

- Protocol version negotiation
- Token authentication
- Session management
- Tool listing and invocation
- MCP call logging

Implemented tools:

- `get_data_availability`
- `search_events`
- `get_sessions`
- `get_daily_summary`
- `ask_home_monitor`

### 4.10 `frontend/`

Responsibility: Frontend admin console.

Tech stack: React 19, TypeScript, Vite, React Query, Zustand, hls.js.

Main pages:

- Login
- Dashboard
- Video source management
- Session list
- Event list and detail
- Daily report list
- Task center
- LLM Provider configuration
- Webhook configuration
- Family profile
- System configuration
- Chat Q&A
- Onboarding wizard

Nginx forwards `/api/`, `/mcp`, and `/health` to the backend.

## 5. Core Business Workflows

### 5.1 Video Source Scanning and Session Building

Core code:

- `src/tasks/session_build.py`
- `src/services/session_build/runner.py`
- `src/adapters/xiaomi_parser.py`

Workflow:

1. `task_maintenance.heartbeat` iterates over all enabled, non-paused video sources every 60 seconds
2. If no active scan task of the same type exists for a video source, a hot scan task is dispatched
3. `session_build.runner.run()` invokes `XiaomiDirectoryParser.scan_directory()` through the discovery stage
4. `VideoFile.file_path_hash` is used for deduplication, preventing duplicate ingestion and analysis
5. New files are appended to the current open session in chronological order; if the gap between adjacent clips exceeds 61 seconds, a new session is created
6. In hot scan mode, only the most recent open session is kept; older open sessions are sealed
7. If an open session receives no new clips within 600 seconds, it is also sealed
8. After sealing, an analysis task is automatically dispatched

Key rules:

- Merge threshold: `MERGE_GAP_SECONDS = 1`
- Seal buffer: `SEAL_BUFFER_SECONDS = 600`
- Scan modes: `hot` / `full`

### 5.2 Session Analysis and Event Generation

Core code:

- `src/tasks/analyzer.py`
- `src/services/session_analysis_video.py`
- `src/services/video_analysis/output_parser.py`
- `src/services/video_analysis/mapper.py`

Workflow:

1. Tasks can only claim a session from `SEALED` state, transitioning it to `ANALYZING`
2. The session is sliced by `ANALYZER_SEGMENT_SECONDS`, defaulting to 600 seconds (10-min session-level chunk)
3. Each session chunk is sub-divided by `ANALYZER_LLM_CHUNK_SECONDS` (default 60 seconds) into sub-chunks; one LLM call per sub-chunk
4. The LLM payload is fixed to `raw_mp4`: each sub-chunk is sent as base64 with `media_io_kwargs.video.num_frames` for server-side uniform sampling. The client no longer retains keyframe decoding, MAD/pHash, or JPEG preprocessing paths.
5. Build LLM prompt per sub-chunk (preserve sub-chunk offset; `base_offset_seconds = sub_chunk.start_offset_seconds`)
6. Call OpenAI-compatible vision model (payload injected via `chat_completion(..., extra_body={...})`)
7. The returned JSON is parsed and converted into multiple `EventRecord` entries (offsets are absolute session-time, non-negative)
8. Previous events for the session are replaced (overwrite strategy)
9. Segment summaries are aggregated and written back to `VideoSession`
10. Analysis status is updated to `SUCCESS`

Analysis and checkpoint/resume semantics:

- Tasks claim a session via `_claim_session_for_analysis`: an atomic conditional status
  update (`transition_session`'s `UPDATE ... WHERE id AND status IN ('sealed','partial')`)
  followed by a commit; the subsequent SELECT only fetches the row back. This is an atomic
  compare-and-set, not a select-then-update, so there is no concurrent-claim race.
- Progress for each sub-chunk is persisted to a durable `SessionAnalysisCheckpoint` (unique
  work key per session + analysis_run + sub_chunk, storing input fingerprint, state, event
  payload, token usage, and error). On mid-run failure the session enters `PARTIAL`; a rerun
  resumes from the first non-success checkpoint and never re-charges successful chunks. Token
  usage is attributed per session + checkpoint in `LLMUsageLog`.
- The finalize step merges completed checkpoints into the session-visible event set; daily
  summaries, Q&A, and MCP consume only sessions with `analysis_status == 'success'`.

Additional mechanisms:

- Task log binding and status persistence; detail_json records sub-chunk planning and execution results
- Token quota checking and token usage recording
- Deadlock retry and analysis status rollback
- On failure, the raw model response summary fragments are preserved for debugging
- ffmpeg stderr tail bounded to last 1KB to keep TaskLog clean

### 5.3 Daily Family Report Generation

Core code:

- `src/tasks/summarizer.py`
- `src/services/summarizer/`
- `src/application/prompt/compiler.py`

Workflow:

1. Beat checks every 60 seconds whether it is time to generate the daily report
2. By default, the report covers "yesterday's" data
3. Events for the target day are queried from `EventRecord`
4. Family profile data is combined to extract known subjects, topic mappings, and attention item candidates
5. If the prompt size is small, `single_pass` mode is used
6. If the prompt size is large, `split_serial` mode is used: summaries are first generated per subject, then an overall summary is produced
7. Results are trimmed to a display-friendly length range
8. The report is published by `summary_date`, while `DailySummaryGenerationAttempt` retains every attempt
9. One targeted webhook outbox event is created per matching subscriber for `daily_summary_generated`

Protection mechanisms:

- Dispatch guard in `AppRuntimeState` prevents duplicate dispatch within the same minute
- No regeneration occurs if a summary already exists or a task is already running

### 5.4 Natural Language Q&A

Core code:

- `src/api/v1/endpoints/chat.py`
- `src/application/qa/service.py`
- `src/application/qa/planner.py`
- `src/application/qa/retriever.py`
- `src/application/qa/evidence_compressor.py`

Workflow:

1. Receives a question text
2. Selects a QA Provider
3. Builds family context
4. Uses the LLM to produce a QueryPlan / RetrievalPlan
5. Performs layered retrieval across daily reports, sessions, and events
6. Compresses evidence to reduce context overhead
7. Calls the model again to generate the final answer
8. Records a `ChatQueryLog`

Characteristics:

- Not a simple full-text search; follows a "understand question → plan retrieval → organize evidence → answer" pipeline
- Can return referenced events and sessions

### 5.5 Webhook Push

Core code:

- `src/api/v1/endpoints/webhooks.py`
- `src/tasks/webhook.py`
- `src/services/webhook_payload.py`
- `src/services/webhook_subscription.py`

Workflow:

1. Users configure webhook URLs, enable status, and subscribed events in the admin console
2. The system constructs standardized payloads at event trigger points
3. Celery tasks deliver HTTP POST requests asynchronously
4. Subscription matching logic supports standardized `event_subscriptions_json`

### 5.6 MCP Capabilities

Core code:

- `src/mcp/server.py`
- `src/mcp/tools.py`
- `src/application/mcp/service.py`

Characteristics:

- Uses a JSON-RPC style interface
- Supports initialization, protocol negotiation, sessions, tool listing, and tool invocation
- Suitable as a stable tool interface layer for agents and bots

## 6. Key Data Model Relationships

Main pipeline:

```text
VideoSource 1 --- n VideoFile
VideoSource 1 --- n VideoSession
VideoSession 1 --- n VideoSessionFileRel --- n VideoFile
VideoSession 1 --- n EventRecord
DailySummary 1 --- 1 summary_date
```

Model descriptions:

- `VideoSource`
  - Describes a recording source
  - Contains name, location, type, configuration, enabled status, validation result
- `VideoFile`
  - A single recording file
  - Deduplicated by path hash
- `VideoSession`
  - A logical session formed from consecutive recording clips
  - Holds analysis status, summary, activity level, subjects, and other fields
- `EventRecord`
  - A structured event recording time, action, subject, importance level, offset, etc.
- `DailySummary`
  - Structured daily summary result
- `TaskLog`
  - Records task status, target, message, retries, and queue task ID (business run lifecycle)
- `OutboxEvent` (transactional outbox, 1:1 with `TaskLog`)
  - Records the publish intent: Celery task name, queue, payload, status
    (pending/publishing/published/failed), retries and lease
  - The standalone publisher uses its `event_id` as the broker `task_id` (ADR 0011)
- `DailySummaryGenerationAttempt`
  - Append-only record of each daily-summary generation attempt per `summary_date`
    (including failure/timeout reasons)
- `PipelineTransitionLog`
  - Append-only audit of the pipeline state machine (`VideoSession` / `TaskLog`); survives
    the 7-day `TaskLog` cleanup
- `SessionAnalysisCheckpoint`
  - Analysis checkpoint-resume and progress; source of the `/metrics` checkpoint progress
- `AppRuntimeState`
  - Runtime protection state; also stores heartbeat metric snapshots
    (`heartbeat_last_counters`)
- `LLMUsageLog`
  - LLM token usage accounting

## 7. Deployment Architecture

### 7.1 Docker Compose Services

`docker-compose.yml` defines the following services:

- `postgres`
  - PostgreSQL 17
- `redis`
  - Redis, serving as Celery broker and result backend
- `backend`
  - FastAPI + Uvicorn
- `celery_worker`
  - Executes scan, daily-report, webhook, and maintenance tasks on the general queue
- `celery_vision_worker`
  - Consumes `analysis_hot` and `analysis_full` with `concurrency=1`
- `celery_beat`
  - Scheduled dispatch of heartbeat and daily report tasks
- `frontend`
  - Nginx hosting frontend static assets and proxying backend APIs

### 7.2 Container Startup Notes

- Backend image is based on `python:3.10-slim`
- `ffmpeg` is installed in the image
- Frontend image uses a two-stage build: Node for building, Nginx for serving
- `backend` depends on `postgres` and `redis` health checks
- `celery_worker` / `celery_vision_worker` / `celery_beat` / `outbox_publisher` depend on the `backend` health check

### 7.3 Volume Mounts

- `./xiaomi_video:/data/videos`
- `./data:/data`
- `./postgres_data:/var/lib/postgresql/data`
- `./redis_data:/data`

Where:

- `/data/videos`: Raw surveillance recordings
- `/data/hls`: Session HLS playback cache

### 7.4 Environment Variables

Key environment variables:

- `APP_ENV` (`production` enforces that `SECRET_KEY` / `MEDIA_SIGNING_KEY` differ and
  validates the `PROVIDER_KEY_ENCRYPTION_KEY` format)
- `DATABASE_URL`
- `REDIS_URL`
- `SECRET_KEY`
- `MEDIA_SIGNING_KEY` (media signing key, must differ from `SECRET_KEY`)
- `PROVIDER_KEY_ENCRYPTION_KEY` (`v1:<Fernet key>` format, at-rest encryption for provider API keys)
- `MCP_TOKEN`
- `VIDEO_ROOT_PATH`
- `PLAYBACK_CACHE_ROOT`
- `DEFAULT_ADMIN_USERNAME`
- `DEFAULT_ADMIN_PASSWORD`
- `DB_INIT_MAX_RETRIES`
- `DB_INIT_RETRY_INTERVAL_SECONDS`
- `DEFAULT_LOCALE`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `ENTITY_IMAGE_ROOT`
- `SESSION_PLAYBACK_MODE`
- `MANIFEST_TTL_SECONDS` / `SEGMENT_TTL_SECONDS` (media signed-URL TTL, default 1800 seconds)
- `WEBHOOK_PRIVATE_NETWORK_ALLOWLIST` (Webhook SSRF-protection intranet allowlist)
- `ANALYZER_SEGMENT_SECONDS`
- `ANALYZER_LLM_CHUNK_SECONDS` (default 60; sub-chunk duration per LLM call)

Note: The root `.env.example` has been updated to reflect the actual PostgreSQL, Redis, MCP, and playback cache directory configuration. Refer to `src/core/config.py` and `docker-compose.yml` as the authoritative runtime references.

Development and deployment toolchain conventions:

- Python version is standardized at 3.10
- Alembic migrations have been verified to upgrade from baseline to `head` on an empty PostgreSQL database

## 8. Frontend Pages to Backend Capability Mapping

- Dashboard → `dashboard/overview`
- Video source management → `video-sources`
- Session management → `sessions`
- Event management → `events`
- Daily report management → `daily-summaries`
- Task center → `tasks/logs`
- LLM Provider → `providers`
- Webhook → `webhooks`
- Family profile → `home-profile`
- System configuration → `system-config`
- Q&A → `chat/ask`
- Onboarding wizard → `onboarding/status`

## 9. Runtime Key Mechanisms

### 9.1 Idempotency and Deduplication

- Video files are deduplicated by `file_path_hash`
- Tasks are protected from duplicate dispatch via `TaskLog + dedupe_key + singleton guard`
- Daily reports use `summary_date` unique constraint for upsert

### 9.2 State Machine

Session analysis states (`analysis_status`) include:

- `open`
- `sealed`
- `analyzing`
- `partial` (partial analysis: some checkpoints succeeded but not all; resumable)
- `success`
- `failed`

`pipeline_state.py` centralizes legal `VideoSession` and `TaskLog` transitions and writes
an append-only `PipelineTransitionLog` audit. Scan builds and task logs retain their own state sets.

### 9.3 Media Lifecycle and Source Deletion (FK Policy)

The media models follow these FK deletion rules (migration `20260826_0017`):

- `video_file.source_id` -> `video_source`: `CASCADE`
- `video_source_runtime_state.source_id` -> `video_source`: `CASCADE`
- `video_session.source_id` -> `video_source`: `RESTRICT`
- `event_record.source_id` -> `video_source`: `RESTRICT` (event history is retained; deleting
  a video source while retained history exists is rejected with business code 4004)
- `event_record.session_id` -> `video_session`: `CASCADE`
- `video_session_file_rel` (session_id / video_file_id) -> `video_session` / `video_file`: `CASCADE`
- `event_tag_rel.event_id` -> `event_record`: `CASCADE`
- `session_analysis_checkpoint.session_id` -> `video_session`: `CASCADE`

Before rewriting the FKs, migration `20260826_0017` runs `_assert_no_existing_orphans()` and
aborts on dangling references. Source deletion is performed by `delete_video_source` per this
policy and is blocked on retained event history.

### 9.4 Dispatch and Source-Scan Serialization + Lease Recovery

- Only one active scan task of a given type is allowed per video source
  (`TaskLog + dedupe_key + singleton guard`), preventing concurrent scans of the same
  directory at the dispatch level.
- Analysis tasks enter the `analysis_hot` / `analysis_full` queues by priority;
  `celery_vision_worker` consumes them serially with concurrency=1, preventing parallel
  analysis within the same queue at the execution level.
- The heartbeat task reclaims orphaned pending tasks and performs timeout recovery (lease
  recovery): runtime states left behind by a crashed process can be re-claimed by the
  heartbeat, which, combined with the atomic claim and analysis checkpoints, enables
  checkpoint resume.

### 9.5 Home Timezone

The `home_timezone` system-config key uses an IANA timezone (default `Asia/Shanghai`,
validated via ZoneInfo; invalid timezone writes are rejected). All timestamps are stored as
timezone-aware UTC; "daily"-boundary logic (e.g. reports) computes the local day range via
`home_timezone`. Migration `20260825_0014` reinterprets legacy naive `Asia/Shanghai`
wall-clock timestamps as UTC instants via `AT TIME ZONE 'Asia/Shanghai'`.

### 9.6 Error Status Middleware

`ResponseStatusMiddleware` maps JSON responses carrying a business code to HTTP statuses
(per `_STATUS_BY_CODE` in `src/api/error_status.py`): 4000→400, 4001→409, 4002→404,
4004→409, 4011→401, 4290→429, 4291→429, 5000→500, 5001→502, 5002→503. The frontend reacts to
HTTP 401 or business code 4011 through the idempotent lock in `authRecovery.ts`, clearing
auth state and redirecting to the login page.

### 9.7 Failure Recovery

- DB initialization supports retry
- Celery tasks support timeout recovery
- Analysis deadlocks support retry
- Orphaned pending tasks can be reclaimed by the heartbeat task

### 9.8 Observability and Operations

- **Structured JSON logging and redaction**: `src/core/logging_config.py` provides
  `configure_logging()` (one-line JSON root handler), `RedactingJsonFormatter`, and
  `redact()` (scrubs secrets, connection strings, sensitive JSON values, and
  `VIDEO_ROOT_PATH` paths).
- **Correlation**: the outbox `event_id` threads through the FastAPI middleware, scheduling
  logs, the outbox publish log, and the Celery `task_prerun` signal, so a single id retrieves
  the full API → outbox → worker chain.
- **Health probes and metrics**: `/livez`, `/readyz` (DB + Redis + alembic head), and
  `GET /metrics` (outbox lag/failures, task-recovery counters, checkpoint progress).
- **Backup/restore**: `scripts/backup_restore_db.py` provides checksummed
  backup / verify / restore for a verified backup before irreversible migrations.
- **Migration serialization**: `init_db` serializes Alembic migration at container bootstrap
  with a PostgreSQL advisory lock (`pg_advisory_lock`).
- **At-most-once vs at-least-once**: delivery is **at-least-once**, defended by consumer-side
  idempotency; nothing here claims exactly-once.

## 10. Testing and Quality Assurance

Backend tests are located in `tests/unit/` and `tests/integration/`.

Common commands:

```bash
python3 -m pytest tests/unit -q         # unit tests
python3 -m pytest -m postgres           # integration tests; requires real PostgreSQL (DATABASE_URL)
python3 -m alembic upgrade head
python3 -m alembic heads                # expect 20260904_0022
ruff check .
ruff format --check src tests
```

Existing test coverage includes:

- QA prompt and retrieval logic
- MCP tools and MCP HTTP interface
- Onboarding HTTP
- Daily summary tasks and output parsing
- Video source validation, webhook subscription, task dispatch binding, etc.

## 11. Current Architecture Characteristics and Recommendations

### Current Characteristics

- The architecture is pragmatic, with a clear main trunk that facilitates rapid iteration
- `api + services + tasks + models` is the actual core organizational pattern
- The `application` layer has developed some abstraction around QA / MCP / pipeline
- Frontend, backend, task system, media playback, and agent interfaces form a complete closed loop

### Recommendations

- `.env.example` should be kept in sync with the current PostgreSQL setup
- `application/bootstrap.py` can gradually evolve into a unified dependency injection entry point
- Media streaming endpoints may benefit from finer-grained authentication in the future (as noted in code comments)
- If more vendor directory formats need to be supported in the future, `adapters/` can be extended accordingly

## 12. One-Line Summary

At its core, this project is a home video intelligent analysis platform built around "directory scanning + asynchronous AI analysis + structured result service delivery".
