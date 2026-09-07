FROM python:3.10-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ARG USE_CN_MIRROR=true

WORKDIR /app

RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
      sed -i "s/deb.debian.org/mirrors.aliyun.com/g" /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY requirements.lock /app/requirements.lock
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
      pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir -r /app/requirements.txt; \
    else \
      pip install --no-cache-dir -r /app/requirements.txt; \
    fi


FROM python:3.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV TZ=Asia/Shanghai

ARG USE_CN_MIRROR=true

WORKDIR /app

RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
      sed -i "s/deb.debian.org/mirrors.aliyun.com/g" /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY src /app/src
COPY supervisord.conf /app/supervisord.conf

EXPOSE 8000

# 单容器部署：supervisord 托管 uvicorn、两个 celery worker、celery beat
# 与 outbox publisher 五个进程（详见 supervisord.conf）。
CMD ["supervisord", "-n", "-c", "/app/supervisord.conf"]
