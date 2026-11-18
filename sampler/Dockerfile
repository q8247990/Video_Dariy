# Frame sampler service (out-of-process frame extraction for the main pipeline).
#
# Wave C / todo 15: scaffold with a /healthz endpoint only. The /sample
# endpoint (ffmpeg frame extraction + ONNX detection) arrives in todo 19.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG USE_CN_MIRROR=true

WORKDIR /app

# ffmpeg: frame extraction (todo 19); curl: container HEALTHCHECK.
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
      sed -i "s/deb.debian.org/mirrors.aliyun.com/g" /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      curl \
      && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY app /app/app

# The dev extra pulls in pytest + httpx (TestClient) so the image can run
# its own test suite (see README "Running the tests").
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
      pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ ".[dev]"; \
    else \
      pip install --no-cache-dir ".[dev]"; \
    fi

EXPOSE 18080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:18080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18080"]
