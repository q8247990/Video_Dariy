# video-dairy-sampler

Out-of-process frame sampling service for Video Diary.

Wave C (todos 15–19) delivers a FastAPI service in its own container that
samples frames from surveillance recordings for the main pipeline. It
answers `/healthz` and `POST /sample` (todo 19): it decodes a whitelisted
video file with ffmpeg at a fixed probe rate, derives event candidate
windows from ffmpeg scene detection (todo 17), selects a bounded set of
frames (todo 18) and returns them as base64 JPEGs.

> Candidate windows come from ffmpeg scene detection only; the ONNX person
> detector was removed until a consumer path exists, so `/sample` is *not*
> person-aware.

The service is deliberately isolated from the main stack: it **never talks
to Postgres or Redis**. It only needs **read access** to the video root(s)
whitelisted by `SAMPLER_ALLOWED_ROOTS`.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SAMPLER_HOST` | `0.0.0.0` | Bind address |
| `SAMPLER_PORT` | `18080` | Bind port |
| `SAMPLER_ALLOWED_ROOTS` | *(empty)* | Colon-separated absolute paths the service may read; empty = nothing allowed |
| `SAMPLER_VERSION` | `0.1.0` | Version reported by `/healthz` |

## HTTP contract

| Method | Path | Response |
|---|---|---|
| GET | `/healthz` | `200 {"status": "ok", "version": "<str>"}` — always 200 |
| POST | `/sample` | `200` with frames (see below); `400` path/validation; `422` undecodable video; `500` unexpected |

### `POST /sample` — request

The request is **bounded by `SAMPLER_ALLOWED_ROOTS`**: `video_path` must be
an absolute, normalised path (no `..`) that resolves — after following
symlinks — to a regular file inside one of the whitelisted roots. Anything
else is rejected with `400` before any bytes are read.

```json
{
  "video_path": "/abs/path.mp4",
  "probe_fps": 2.0,
  "min_frames": 4,
  "max_frames": 120,
  "max_edge": 640,
  "jpeg_quality": 85,
  "mode": "uniform",
  "dedupe_phash_distance": null,
  "window_pre_seconds": 5.0,
  "window_post_seconds": 10.0,
  "max_event_windows": 4,
  "mad_threshold": 1.0,
  "periodic_anchor_seconds": 8.0
}
```

| Field | Type | Default | Constraint | Meaning |
|---|---|---|---|---|
| `video_path` | string | — | absolute, normalised, inside `SAMPLER_ALLOWED_ROOTS`, regular file | video file to decode |
| `probe_fps` | number | `2.0` | `> 0` | frames per second sampled by the two ffmpeg passes |
| `min_frames` | int | `4` | `>= 1` | floor for the budget check; `max_frames < min_frames` → `400` |
| `max_frames` | int | `120` | `>= 1` | frame **budget** passed to `select_frames(budget=...)` |
| `max_edge` | int \| null | `640` | `>= 1` (or `null`) | explicit downscale opt-in: when `< 2560`, frames are scaled in ffmpeg so the long edge `<= max_edge` (never upscaled, even dimensions); `null` / `>= 2560` keeps in-box sources at source resolution |
| `jpeg_quality` | int | `85` | `1..100` | JPEG quality of the returned frames (re-encode when resized) |
| `mode` | string | `"uniform"` | `uniform` \| `dedupe` \| `event` \| `keyframe` | selection strategy |
| `dedupe_phash_distance` | int \| null | `null` | `>= 0` | pHash Hamming threshold for `dedupe` / the pHash change threshold for `keyframe` (`null` = 6) |
| `window_pre_seconds` | number | `5.0` | `>= 0` | candidate window context before a scene trigger |
| `window_post_seconds` | number | `10.0` | `>= 0` | candidate window context after a scene trigger |
| `max_event_windows` | int | `4` | `>= 1` | cap on candidate windows (highest scene scores kept) |
| `mad_threshold` | number | `1.0` | `>= 0` | `keyframe` change detector: mean absolute difference on the 160x90 grayscale probe frame vs the previous one |
| `periodic_anchor_seconds` | number | `8.0` | `>= 0` | `keyframe` periodic anchor interval (`0` disables anchors) |

Unknown fields are ignored. The whole request is bounded by a 120 s
per-ffmpeg-invocation timeout; extraction failures surface as `422` with
the ffmpeg stderr tail.

### Selection modes

- `uniform` — index-proportional even spacing over the whole clip.
- `dedupe` — pHash-diverse frames (greedy dissimilarity walk, then budget fill).
- `event` — up to 70% of the budget on scene-change candidate windows.
- `keyframe` — revives the deleted backend keyframe pipeline as a selection layer:
  each probe frame is compared to the previous one by MAD (160x90 grayscale) or
  pHash Hamming distance; a frame is a change point when `mad > mad_threshold` or
  `phd > dedupe_phash_distance`. Every `periodic_anchor_seconds` forces a periodic
  anchor. Change points are ranked by `mad + phd` and accepted with a minimum
  temporal gap of `duration / budget`; anchors and even-spacing fill guarantee
  whole-clip coverage and exactly `budget` frames. No second ffmpeg pass is made.

### Supported resolution box (1440p)

The sampler supports sources up to the **2560x1440 box** (constants
`SUPPORTED_MAX_WIDTH` / `SUPPORTED_MAX_HEIGHT` in `app/config.py`). The source
dimensions and the container duration are probed in a single ffprobe call
before any decoding:

| Source resolution | `max_edge` | Outcome |
|---|---|---|
| fits the box (`w <= 2560 and h <= 1440`) | `null` or `>= 2560` | **pass-through**: no downscaling, output = source dimensions (odd dimensions aligned to even) |
| fits the box | `< 2560` and `< source long edge` | scaled in ffmpeg to fit the `max_edge` square box (aspect preserved, never upscaled, even dimensions) |
| outside the box (e.g. 4K 3840x2160) | `null` or `>= 2560` | **rejected by default** — fail fast with `422` (`ExtractionFailed`) before decoding; no silent downscaling |
| outside the box | `< 2560` | allowed — explicit downscale opt-in, frames fit the `max_edge` square box |

The reject message names the source size, the supported box and the remedy:
`source resolution 3840x2160 exceeds the supported 2560x1440 box: set
max_edge to a smaller value to opt into downscaling`.

### `POST /sample` — response (200)

```json
{
  "frames": [
    {"t_seconds": 0.0, "jpeg_base64": "<base64 of the JPEG bytes>"}
  ],
  "elapsed_ms": 123
}
```

- `frames`: the selected frames (at most `max_frames`), strictly ascending
  `t_seconds`, each `jpeg_base64` is a complete JPEG (`\xff\xd8…\xff\xd9`),
  long edge `<= max_edge`, aspect ratio preserved.
- `elapsed_ms`: both ffmpeg passes.

Candidate windows are an internal detail of `event` mode: they are derived
from ffmpeg scene detection (todo 17) and consumed by `select_frames`, but
they are not part of the HTTP response.

### `POST /sample` — error codes

| Code | When | `detail` |
|---|---|---|
| `400` | `video_path` outside `SAMPLER_ALLOWED_ROOTS`, relative or `..`-unnormalised path, symlink target outside the roots, or not a regular file | the specific path-safety violation (refers only to the path the request sent) |
| `400` | any schema/validation error (bad type, out-of-range, unknown `mode`, missing field) | list of validation errors |
| `400` | `FrameSelectionError` — `max_frames` below the `min_frames` floor | message containing the minimum frame count |
| `422` | video cannot be decoded (`ExtractionFailed`: corrupt file, decode error, ffmpeg timeout) **or** source outside the supported 2560x1440 box without an explicit `max_edge` downscale opt-in | `ExtractionFailed` message with the ffmpeg stderr tail, or the source size + box + remedy |
| `500` | any unexpected server error | generic `internal error` (traceback logged server-side, never leaked) |

## Build

```bash
docker build -t video-dairy-sampler ./sampler
```

## Run

```bash
docker run --rm -d --name fs-sampler \
  -p 18080:18080 \
  -v /path/to/videos:/videos:ro \
  -e SAMPLER_ALLOWED_ROOTS=/videos \
  video-dairy-sampler

curl -s localhost:18080/healthz
# {"status":"ok","version":"0.1.0"}
```

The video root(s) must be mounted **read-only** into the container; the
sampler never writes into them.

## Docker Compose

The main repo ships the sampler as an **opt-in compose profile**
(`profiles: ["sampler"]` in `docker-compose.yml`), so a plain
`docker compose up` still starts only the default 4-service stack
(postgres / redis / backend / frontend).

From the repo root:

```bash
docker compose --profile sampler up -d --build
curl -s localhost:18080/healthz
# {"status":"ok","version":"0.1.0"}
```

Port `18080` is published host-side for debugging; the backend reaches the
sampler over the compose network at `http://sampler:18080` (no published
port required for that).

**Path parity is mandatory, not convenience — and it involves two different
variables.** The backend sends the DB `video_path` (e.g.
`/data/videos/clip.mp4`) to the sampler verbatim, so the sampler must see the
videos at the **same absolute in-container path** the backend uses:
`/data/videos`, whitelisted via `SAMPLER_ALLOWED_ROOTS=/data/videos`.

- `VIDEO_HOST_PATH` (host side): the directory compose bind-mounts,
  `${VIDEO_HOST_PATH:-./xiaomi_video}` in `docker-compose.yml` — the same
  convention as `docker-compose.release.yml`. It is interpolated on the host,
  so it must be a real host path.
- `VIDEO_ROOT_PATH` (container side): the in-container path the backend and
  the sampler read (`/data/videos`). It must equal the mount target and
  `SAMPLER_ALLOWED_ROOTS`; it is **not** a host path.

Do not use `VIDEO_ROOT_PATH` as the compose volume source: `/data/videos`
does not exist on the host, so the sampler would see an empty video root and
reject every `/sample` request with `400`.

Then enable image mode in two steps — the backend reads **SystemConfig, not
any environment variable**:

1. SystemConfig (settings → system configuration):
   - `frame_sampler_enabled=true`
   - `frame_sampler_url=http://sampler:18080`
2. Provider (settings → model connection): set
   `extra_config_json.frame_sampling` on the provider you want in image mode.

**Failure semantics (no fallback chain):** without a reachable sampler, the
analysis task for an image-mode provider fails the affected file with
`SamplerUnavailable` and the session lands in `PARTIAL`/`FAILED`. The system
does **not** auto-retry sampler outages (auto-retry only covers PG deadlocks
and lease timeouts); re-trigger manually with
`POST /api/v1/tasks/analyze/{session_id}`.

## Running the tests

`pytest` + `httpx` are installed in the image via the `dev` extra. The test
files are excluded from the image (`.dockerignore`), so mount them:

```bash
docker run --rm --entrypoint python \
  -v $(pwd)/sampler/tests:/app/tests \
  video-dairy-sampler -m pytest -q
```
