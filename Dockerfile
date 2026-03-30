FROM python:3.10-slim

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
    build-essential \
    pkg-config \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
      pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir -r /app/requirements.txt; \
    else \
      pip install --no-cache-dir -r /app/requirements.txt; \
    fi

COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY src /app/src

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
