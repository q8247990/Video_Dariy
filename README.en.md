> **中文版**: [README.md](README.md)

# Video Diary — AI-Powered Home Surveillance Video Analysis

> Got security cameras at home and a NAS full of recordings nobody ever watches?
>
> Video Diary watches every clip for you and tells you what happened at home today.

![Dashboard](image.png)

![Events & Playback](image-1.png)

---

## What It Does

- **Automatic video scanning** — Point it at your NAS video directory. The system discovers new recordings, deduplicates, and merges them into complete events by timeline.
- **AI-powered event recognition** — Leverages local or cloud multimodal LLMs to identify scenes like "cat jumped on the table", "delivery person arrived", or "kid came home from school".
- **Daily family digest** — Automatically generates a daily report summarizing all events, highlighting key subjects and items of concern.
- **Natural language Q&A** — Ask questions like "Did anyone linger at the front door for more than 5 minutes last night?" and get answers backed by event data.
- **Webhook / MCP integration** — Connect to WeChat bots, Home Assistant, Claude Desktop, and other external systems.
- **Web admin console** — Manage video sources, event playback, daily reports, family profiles, and model configuration — all through a visual interface.

## Quick Start

**Prerequisites**: Docker + Docker Compose

```bash
# 1. Clone the project
git clone <repo-url> && cd video_dairy

# 2. Update the video directory mount (top of docker-compose.yml)
#    Replace ./xiaomi_video with your camera recording directory

# 3. Start
docker compose up --build -d

# 4. Open your browser
#    http://localhost:8226
```

The default admin account is configured via environment variables (see "Configuration Reference" below). After startup, follow the onboarding wizard to set up video sources and model connections.

## Local LLM Deployment

The system is compatible with any model service that exposes an OpenAI-compatible API. We recommend using vLLM to deploy multimodal models locally for fully offline operation — your data never leaves the LAN.

### Hardware Requirements

| GPU | VRAM | Recommended Models |
|-----|------|--------------------|
| RTX 3090 / 3090 Ti | 24GB | MiniCPM-V 4.5 int4, MiniCPM-o 4.5 AWQ |
| RTX 4090 | 24GB | Same as above, or Qwen3.5-9B |
| Dual GPU / Higher VRAM | 48GB+ | Larger parameter models possible |

### Recommended Models

| Model | Purpose | Link |
|-------|---------|------|
| MiniCPM-V 4.5 int4 | Video understanding (primary) | [ModelScope](https://modelscope.cn/models/OpenBMB/MiniCPM-V-4_5-int4) |
| MiniCPM-o 4.5 AWQ | Video understanding (alternative) | [ModelScope](https://modelscope.cn/models/OpenBMB/MiniCPM-o-4_5-awq) |
| Qwen3.5-9B | Text summarization / daily report generation | [ModelScope](https://modelscope.cn/models/Qwen/Qwen3.5-9B) |

### vLLM Startup Example

```bash
# Install vLLM
pip install vllm

# Start the vision model (using MiniCPM-V 4.5 int4 as an example)
vllm serve OpenBMB/MiniCPM-V-4_5-int4 \
  --trust-remote-code \
  --port 8000 \
  --max-model-len 4096
```

After startup, go to "Settings → Model Connection" in the admin console and enter:

- API URL: `http://<your-IP>:8000/v1`
- Model name: must match the name used when starting vLLM

### Known Limitations

- **Ollama**: Does not support the `video` parameter; cannot be used for video analysis.
- **Bailian Platform**: Uploading videos longer than 1 minute requires a publicly accessible URL, which is not currently supported.

## Deployment

### Docker Compose Full-Stack Deployment (Recommended)

`docker-compose.yml` includes the following services:

| Service | Description |
|---------|-------------|
| postgres | Application database |
| redis | Celery message broker |
| backend | FastAPI backend |
| celery_worker | Async task execution |
| celery_beat | Scheduled task dispatch |
| frontend | React frontend + Nginx reverse proxy |

```bash
docker compose up --build -d    # Start
docker compose ps               # Check status
docker compose logs -f backend   # View backend logs
docker compose down              # Stop
```

### Offline Delivery Packaging

Suitable for NAS / intranet environments. Build images and export a delivery package:

```bash
bash scripts/package_release.sh --tag v1.0.0
```

Output is located at `output/<tag>/`, containing image archives and a streamlined `docker-compose.yml`. Users only need to change one video directory mount to get running.

### Configuration Reference

### Production Key Lifecycle

Production deployments use `APP_ENV=production`. On startup you must inject three mutually
distinct values from the environment: `SECRET_KEY`, `MEDIA_SIGNING_KEY`, and a versioned
`PROVIDER_KEY_ENCRYPTION_KEY`. Missing or known-default values block startup, and no key
content is ever logged. `APP_ENV=production` also enforces that `SECRET_KEY` and
`MEDIA_SIGNING_KEY` differ, and that `PROVIDER_KEY_ENCRYPTION_KEY` is in `v1:<Fernet key>`
format. Generate each value independently:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
python3 -c "from cryptography.fernet import Fernet; print('v1:' + Fernet.generate_key().decode())"
```

Store the generated values in a secrets manager or a `.env` file excluded from version
control. Rotating app signing keys follows the corresponding business release plan. To
rotate Provider crypto material, take a verified DB backup first, then run with the new
`PROVIDER_KEY_ENCRYPTION_KEY`:

```bash
OLD_PROVIDER_KEY_ENCRYPTION_KEY='<old v1 key>' python3 scripts/rotate_provider_key_encryption.py
```

That script only accepts encrypted records and re-encrypts them in a transaction; the only
recovery path for a failed migration or rotation is restoring that backup. Provider API keys
are stored as ciphertext only; responses return a mask. Keyless local vLLM providers (empty
`api_key`) remain fully supported.

| Variable | Description | Must Change? |
|----------|-------------|--------------|
| `VIDEO_ROOT_PATH` | Video recording directory (container path) | Mounted via docker-compose |
| `SECRET_KEY` | JWT signing key | Required for production |
| `MEDIA_SIGNING_KEY` | Media URL signing key, must differ from `SECRET_KEY` | Required for production |
| `PROVIDER_KEY_ENCRYPTION_KEY` | Provider API-key at-rest encryption key, `v1:<Fernet key>` format | Required for production |
| `DEFAULT_ADMIN_USERNAME` | Default admin username | Recommended |
| `DEFAULT_ADMIN_PASSWORD` | Default admin password | Recommended |
| `DATABASE_URL` | PostgreSQL connection string | Keep default for Docker |
| `REDIS_URL` | Redis connection string | Keep default for Docker |
| `PLAYBACK_CACHE_ROOT` | HLS playback cache directory | Keep default for Docker |
| `MCP_TOKEN` | MCP API authentication token | Configure when using MCP |

### Media Signed URLs and Expiry

Media playback addresses (raw file streams, session merged streams, HLS manifests and
segments, entity images) are signed URLs carrying a scoped capability (resource kind,
resource id, request method, expiry, parent session) verified via HMAC-SHA256. Responses set
`Cache-Control: no-store`. Manifest and segment URLs are valid for 1800 seconds
(`MANIFEST_TTL_SECONDS` / `SEGMENT_TTL_SECONDS`), after which they expire and must be
re-acquired. Cross-session access is rejected by the scope guard.

### Webhook Outbound Security (SSRF Protection)

Outbound webhook URLs reject localhost / private ranges / `169.254.169.254` / URLs carrying
credentials, and are re-resolved at delivery time to guard against DNS rebinding; redirects
are not followed. Use `WEBHOOK_PRIVATE_NETWORK_ALLOWLIST` to explicitly allowlist specific
intranet addresses. Every delivery outcome is recorded in `WebhookDeliveryLog`.

### Home Timezone and Migration Backup

The `home_timezone` system-config key uses an IANA timezone (default `Asia/Shanghai`,
validated via ZoneInfo). All timestamps are stored as timezone-aware UTC. Migration
`20260825_0014` reinterprets legacy naive `Asia/Shanghai` wall-clock timestamps as UTC
instants via `AT TIME ZONE 'Asia/Shanghai'`.

**Take a verified DB backup before migrating**: `20260825_0012` (provider-key encryption),
`20260825_0014` (timestamps to UTC), and `20260826_0017` (FK deletion policy) are all
irreversible (downgrade raises); `20260826_0017` also aborts if orphaned references are
found. After any failed migration or rotation, restoring that backup is the only recovery
path.

Backup / verify / restore in one pass (`pg_dump` / `pg_restore` must match the server major
version):

```bash
python3 -m scripts.backup_restore_db backup --database-url "$DATABASE_URL" --output /tmp/hm.dump
python3 -m scripts.backup_restore_db verify --output /tmp/hm.dump
python3 -m scripts.backup_restore_db restore --database-url "<temp-db-url>" --dump /tmp/hm.dump
```

### Observability and Operations (structured logs, metrics, backup/restore)

- **Structured JSON logs**: `configure_logging()` at process entry installs a root handler
  emitting one-line JSON per record (`timestamp/level/logger/correlation_id/message`) and
  redacts at the log boundary. `redact()` scrubs known secret values, `DATABASE_URL` /
  `REDIS_URL` connection strings, `api_key` / `token` / `password` JSON values, and file
  paths rooted at `VIDEO_ROOT_PATH` / `PLAYBACK_CACHE_ROOT`.
- **Correlation**: the outbox `event_id` is the correlation key. The FastAPI middleware
  assigns an `X-Request-ID`; scheduling logs, the outbox publish log, and the Celery worker
  log (`task_id == event_id`) carry the same `correlation_id`, so a single id retrieves the
  whole API → outbox → worker chain.
- **Delivery semantics**: **at-least-once**, never exactly-once. Duplicate publishes are
  possible and the consumer-side `bind_or_create_running_task_log` idempotency short-circuit
  is the defence.
- **Health and metrics**: `/livez` (liveness), `/readyz` (DB + Redis + alembic head), and
  `GET /metrics` (JSON: outbox lag/failure counts, task-recovery counters, checkpoint
  progress for running analyses).
- **Transactional outbox and task lifecycle**: `TaskLog` owns the business run lifecycle,
  `OutboxEvent` owns the publish intent (1:1, committed atomically), and a standalone
  publisher process drives the broker. `DailySummaryGenerationAttempt` records daily-summary
  attempts (append-only); `PipelineTransitionLog` is an append-only pipeline state-machine
  audit that survives the 7-day `TaskLog` cleanup. See ADR `docs/adr/0011-*` and
  `docs/adr/0012-*`.

### Partial Analysis and Checkpoint Resume

During analysis, progress for each sub-chunk is persisted to a durable
`SessionAnalysisCheckpoint` (unique work key per session + analysis_run + sub_chunk, storing
input fingerprint, state, event payload, token usage, and error). On mid-run failure the
session enters `PARTIAL`; a rerun resumes from the first non-success checkpoint and never
re-charges successful chunks. Token usage is attributed per session + checkpoint in
`LLMUsageLog`. Daily summaries, Q&A, and MCP consume only sessions with
`analysis_status == 'success'`.

### Source File Lifecycle and Deletion Policy

Missing source files are marked `file_missing` / `missing_at` (paths confined to
`VIDEO_ROOT_PATH`, rejecting `..` and non-absolute paths); the row and its relationships are
retained for provenance. Playback reports per-file `available` / `unavailable_reason` /
`missing_at` plus session-level `availability` (available / partial / unavailable); HLS
manifests include only available segments and merged video skips missing segments.

Deleting a video source applies this history-retention policy (FK rules from migration
`20260826_0017`): `video_file` CASCADE, `video_source_runtime_state` CASCADE,
`video_session` RESTRICT, and `event_record.source_id` RESTRICT (retained event history
blocks deletion with business code 4004); `event_record.session_id`,
`video_session_file_rel`, `event_tag_rel.event_id`, and `session_analysis_checkpoint.session_id`
CASCADE.

### HTTP Business Error Code Mapping

A unified response middleware maps business codes to HTTP statuses: 4000→400, 4001→409,
4002→404, 4004→409, 4011→401, 4290→429, 4291→429, 5000→500, 5001→502, 5002→503. The frontend
reacts to HTTP 401 or business code 4011 by clearing auth state through an idempotent lock and
redirecting to the login page.

### Default Access URLs

- Frontend: `http://localhost:8226`
- Health check: `http://localhost:8226/health`
- MCP endpoint: `http://localhost:8226/mcp`

---

<details>
<summary><strong>Developer Reference</strong></summary>

### Tech Stack

**Backend**: FastAPI / Celery / SQLAlchemy + Alembic / PostgreSQL / Redis / httpx / ffmpeg

**Frontend**: React 19 + TypeScript + Vite / React Router / @tanstack/react-query / Zustand / hls.js / Nginx

### Core Pipeline

```text
Video Directory → VideoSource → VideoFile → VideoSession → EventRecord → DailySummary
                                                         → Chat / MCP / Webhook
```

1. `heartbeat` dispatches a hot scan for each active video source every 60 seconds
2. Scans directories, parses timestamps, deduplicates by file hash, and writes to `video_file`
3. Consecutive clips are merged into `video_session` based on time gap
4. Sealed sessions are dispatched for AI analysis, producing `event_record`
5. `daily_summary` is generated on schedule or manually
6. Frontend, Q&A, MCP, and Webhook capabilities are built on top of the structured results

### Directory Structure

```text
.
├── src/
│   ├── api/                  # FastAPI routes and dependencies
│   ├── application/          # Orchestration, QA, MCP, prompt assembly
│   ├── core/                 # Configuration, security, Celery
│   ├── db/                   # Session / Alembic initialization
│   ├── infrastructure/       # Task dispatch, LLM gateway adapters
│   ├── mcp/                  # MCP server and tool implementations
│   ├── models/               # SQLAlchemy models
│   ├── providers/            # OpenAI-compatible client
│   ├── services/             # Core business services
│   └── tasks/                # Celery tasks
├── frontend/                 # Frontend project
├── alembic/                  # Database migrations
├── tests/                    # Unit / integration tests
├── docker-compose.yml
└── Dockerfile
```

For a more complete architecture overview, see [ARCHITECTURE.en.md](ARCHITECTURE.en.md).

**Audit governance**: the full audit finding register is maintained in the working file
`docs/finding-register.md` (per repo convention `docs/` is gitignored and tracked only
internally, not committed). Operators and secondary developers can consult it to trace the
status and owner of past findings.

### Local Development

```bash
# Backend (requires PostgreSQL + Redis)
pip install -r requirements.txt
python -m src.main

# Frontend
cd frontend
npm ci
npm run dev
```

Python version: 3.10

### Key API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/auth/login` | Login |
| GET | `/api/v1/dashboard/overview` | Dashboard statistics |
| GET | `/api/v1/video-sources` | Video source list |
| POST | `/api/v1/tasks/{id}/build/full` | Full build |
| POST | `/api/v1/tasks/analyze/{session_id}` | Manual analysis |
| POST | `/api/v1/tasks/summarize` | Manual daily report generation |
| POST | `/api/v1/chat/ask` | Natural language Q&A |
| GET | `/api/v1/daily-summaries` | Daily report list |
| GET | `/api/v1/events` | Event list |
| POST | `/mcp` | MCP JSON-RPC endpoint |

### Common Commands

```bash
# Reset scan/session/event/task data
docker compose exec backend python -m src.reset_pipeline_data

# Backend unit tests (661)
python3 -m pytest tests/unit -q

# Backend integration tests (requires real PostgreSQL, DATABASE_URL)
python3 -m pytest -m postgres

# Migration checks
python3 -m alembic upgrade head
python3 -m alembic heads   # expect 20260902_0021

# Code checks
ruff check .
ruff format --check src tests
# Known exceptions: ruff format --check has 4 pre-existing out-of-scope failures:
#   src/application/prompt/compiler.py, src/application/qa/agent.py,
#   tests/unit/test_i18n.py, tests/unit/test_keyframe_extractor.py
```

</details>

---

<details>
<summary><strong>More Screenshots</strong></summary>

![Pet Profile](image-2.png)

![Q&A Page](image-3.png)

![Family Profile](image-4.png)

</details>
