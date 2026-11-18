# video-dairy-sampler

Out-of-process frame sampling service for Video Diary.

Wave C (todo 15) delivers the scaffold: a FastAPI service in its own
container that will sample frames from surveillance recordings for the main
pipeline. The detection/sampling logic (`/sample`) arrives in todo 19; this
image only answers `/healthz`.

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
| POST | `/sample` | **todo 19** — not implemented in this scaffold |

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
