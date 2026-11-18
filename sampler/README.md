# video-dairy-sampler

Out-of-process frame sampling service for Video Diary.

Wave C (todos 15–19) delivers a FastAPI service in its own container that
samples frames from surveillance recordings for the main pipeline. It
answers `/healthz` and `POST /sample` (todo 19): it decodes a whitelisted
video file with ffmpeg at a fixed probe rate, derives event candidate
windows from ffmpeg scene detection (todo 17), selects a bounded set of
frames (todo 18) and returns them as base64 JPEGs.

> **Honesty note — the ONNX person detector and tracker (todo 16) are
> implemented and unit-tested but NOT yet used by `/sample`; candidate
> windows currently come from ffmpeg scene detection (todo 17).** `/sample`
> is *not* person-aware.

The service is deliberately isolated from the main stack: it **never talks
to Postgres or Redis**. It only needs **read access** to the video root(s)
whitelisted by `SAMPLER_ALLOWED_ROOTS`.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SAMPLER_HOST` | `0.0.0.0` | Bind address |
| `SAMPLER_PORT` | `18080` | Bind port |
| `SAMPLER_ALLOWED_ROOTS` | *(empty)* | Colon-separated absolute paths the service may read; empty = nothing allowed |
| `SAMPLER_MODEL_PATH` | *(empty)* | Absolute path to the detection model file; unset/missing → `model_loaded: false` (service still starts) |
| `SAMPLER_VERSION` | `0.1.0` | Version reported by `/healthz` |

## HTTP contract

| Method | Path | Response |
|---|---|---|
| GET | `/healthz` | `200 {"status": "ok", "model_loaded": <bool>, "version": "<str>"}` — always 200 even when the model file is missing |
| POST | `/sample` | `200` with frames + candidates (see below); `400` path/validation; `422` undecodable video; `500` unexpected |

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
  "max_event_windows": 4
}
```

| Field | Type | Default | Constraint | Meaning |
|---|---|---|---|---|
| `video_path` | string | — | absolute, normalised, inside `SAMPLER_ALLOWED_ROOTS`, regular file | video file to decode |
| `probe_fps` | number | `2.0` | `> 0` | frames per second sampled by the two ffmpeg passes |
| `min_frames` | int | `4` | `>= 1` | floor for the budget check; `max_frames < min_frames` → `400` |
| `max_frames` | int | `120` | `>= 1` | frame **budget** passed to `select_frames(budget=...)` |
| `max_edge` | int | `640` | `>= 1` | selected frames are downscaled so the long edge `<= max_edge` (never upscaled) |
| `jpeg_quality` | int | `85` | `1..100` | JPEG quality of the returned frames (re-encode when resized) |
| `mode` | string | `"uniform"` | `uniform` \| `dedupe` \| `event` | selection strategy (todo 18) |
| `dedupe_phash_distance` | int \| null | `null` | `>= 0` | pHash Hamming threshold for `dedupe` (`null` = 6) |
| `window_pre_seconds` | number | `5.0` | `>= 0` | candidate window context before a scene trigger |
| `window_post_seconds` | number | `10.0` | `>= 0` | candidate window context after a scene trigger |
| `max_event_windows` | int | `4` | `>= 1` | cap on candidate windows (highest scene scores kept) |

Unknown fields are ignored. The whole request is bounded by a 120 s
per-ffmpeg-invocation timeout; extraction failures surface as `422` with
the ffmpeg stderr tail.

### `POST /sample` — response (200)

```json
{
  "frames": [
    {"t_seconds": 0.0, "jpeg_base64": "<base64 of the JPEG bytes>"}
  ],
  "candidates": [
    {"t_start": 5.0, "t_end": 20.0, "reasons": ["scene"]}
  ],
  "elapsed_ms": 123,
  "duration_seconds": 60.0
}
```

- `frames`: the selected frames (at most `max_frames`), strictly ascending
  `t_seconds`, each `jpeg_base64` is a complete JPEG (`\xff\xd8…\xff\xd9`),
  long edge `<= max_edge`, aspect ratio preserved.
- `candidates`: scene-detection candidate windows (ffmpeg scene scores,
  todo 17) with `reasons` = `["scene"]` (single trigger) or `["merged"]`
  (merged triggers). **Not person-aware — see the honesty note above.**
- `elapsed_ms`: both ffmpeg passes. `duration_seconds`: container duration
  (best-effort ffprobe, 0.0 when unknown, last-frame fallback).

### `POST /sample` — error codes

| Code | When | `detail` |
|---|---|---|
| `400` | `video_path` outside `SAMPLER_ALLOWED_ROOTS`, relative or `..`-unnormalised path, symlink target outside the roots, or not a regular file | the specific path-safety violation (refers only to the path the request sent) |
| `400` | any schema/validation error (bad type, out-of-range, unknown `mode`, missing field) | list of validation errors |
| `400` | `FrameSelectionError` — `max_frames` below the `min_frames` floor | message containing the minimum frame count |
| `422` | video cannot be decoded (`ExtractionFailed`: corrupt file, decode error, ffmpeg timeout) | `ExtractionFailed` message with the ffmpeg stderr tail |
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
  -e SAMPLER_MODEL_PATH=/models/yolov8n.onnx \
  video-dairy-sampler

curl -s localhost:18080/healthz
# {"status":"ok","model_loaded":true,"version":"0.1.0"}
```

The video root(s) must be mounted **read-only** into the container; the
sampler never writes into them.

## Running the tests

`pytest` + `httpx` are installed in the image via the `dev` extra. The test
files are excluded from the image (`.dockerignore`), so mount them:

```bash
docker run --rm --entrypoint python \
  -v $(pwd)/sampler/tests:/app/tests \
  video-dairy-sampler -m pytest -q
```
