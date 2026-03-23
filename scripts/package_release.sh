#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_ROOT="${ROOT_DIR}/output"

usage() {
  cat <<'EOF'
用法:
  scripts/package_release.sh [--tag <release_tag>]

说明:
  在项目根目录 output/<release_tag>/ 下生成交付包，包含:
  - images/*.tar(.gz)
  - docker-compose.yml
  - README.md

可选参数:
  --tag  指定发布标签，不传默认使用时间戳 (YYYYMMDD_HHMMSS)
EOF
}

RELEASE_TAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      if [[ $# -lt 2 ]]; then
        echo "错误: --tag 缺少参数" >&2
        exit 1
      fi
      RELEASE_TAG="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "错误: 不支持的参数 $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${RELEASE_TAG}" ]]; then
  RELEASE_TAG="$(date +%Y%m%d_%H%M%S)"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "错误: 未找到 docker 命令" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "错误: 当前环境不可用 docker compose" >&2
  exit 1
fi

if [[ ! -f "${ROOT_DIR}/docker-compose.yml" ]]; then
  echo "错误: 未找到 ${ROOT_DIR}/docker-compose.yml" >&2
  exit 1
fi

PACKAGE_DIR="${OUTPUT_ROOT}/${RELEASE_TAG}"
IMAGES_DIR="${PACKAGE_DIR}/images"

if [[ -e "${PACKAGE_DIR}" ]]; then
  echo "错误: 输出目录已存在: ${PACKAGE_DIR}" >&2
  exit 1
fi

mkdir -p "${IMAGES_DIR}"

BACKEND_IMAGE="video_dairy_backend:${RELEASE_TAG}"
FRONTEND_IMAGE="video_dairy_frontend:${RELEASE_TAG}"
POSTGRES_IMAGE="postgres:17-alpine"
REDIS_IMAGE="redis:alpine"

echo "[1/7] 构建 backend 镜像: ${BACKEND_IMAGE}"
docker build -t "${BACKEND_IMAGE}" "${ROOT_DIR}"

echo "[2/7] 构建 frontend 镜像: ${FRONTEND_IMAGE}"
docker build -t "${FRONTEND_IMAGE}" "${ROOT_DIR}/frontend"


echo "[4/7] 导出镜像 tar.gz"
docker save "${BACKEND_IMAGE}" | gzip > "${IMAGES_DIR}/backend_${RELEASE_TAG}.tar.gz"
docker save "${FRONTEND_IMAGE}" | gzip > "${IMAGES_DIR}/frontend_${RELEASE_TAG}.tar.gz"
docker save "${POSTGRES_IMAGE}" | gzip > "${IMAGES_DIR}/postgres_17-alpine.tar.gz"
docker save "${REDIS_IMAGE}" | gzip > "${IMAGES_DIR}/redis_alpine.tar.gz"

echo "[5/7] 生成镜像清单"
cat > "${IMAGES_DIR}/images.txt" <<EOF
${BACKEND_IMAGE}
${FRONTEND_IMAGE}
${POSTGRES_IMAGE}
${REDIS_IMAGE}
EOF

(
  cd "${IMAGES_DIR}"
  sha256sum ./*.tar.gz > sha256sum.txt
)

echo "[6/7] 生成客户部署 docker-compose.yml"
cat > "${PACKAGE_DIR}/docker-compose.yml" <<EOF
# 只需修改下面这一处视频目录挂载
x-video-source-mount: &video-source-mount "./xiaomi_video:/data/videos"

# 定义公共环境变量块（硬编码）
x-common-env: &common-env
  TZ: Asia/Shanghai
  DATABASE_URL: postgresql+psycopg://postgres:123456@postgres:5432/home_monitor
  REDIS_URL: redis://redis:6379/0
  SECRET_KEY: supersecretkey_please_change_in_production
  VIDEO_ROOT_PATH: /data/videos
  PLAYBACK_CACHE_ROOT: /data/hls
  DEFAULT_ADMIN_USERNAME: admin
  DEFAULT_ADMIN_PASSWORD: 123456

# 定义公共 extra_hosts
x-common-extra-hosts: &common-extra-hosts
  - "host.docker.internal:host-gateway"

# 其他挂载路径锚点
x-volume-data: &volume-data "./data:/data"

services:
  postgres:
    image: postgres:17-alpine
    container_name: hm_postgres
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: 123456
      POSTGRES_DB: home_monitor
    volumes:
      - ./postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d home_monitor"]
      interval: 5s
      timeout: 5s
      retries: 30
      start_period: 20s
    networks:
      - app_bridge

  redis:
    image: redis:alpine
    container_name: hm_redis
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
    volumes:
      - ./redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 5s
    networks:
      - app_bridge

  backend:
    image: ${BACKEND_IMAGE}
    container_name: hm_backend
    restart: unless-stopped
    environment:
      <<: *common-env
      DB_INIT_MAX_RETRIES: 180
      DB_INIT_RETRY_INTERVAL_SECONDS: 2
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - *video-source-mount
      - *volume-data
    extra_hosts: *common-extra-hosts
    command: uvicorn src.main:app --host 0.0.0.0 --port 8000
    healthcheck:
      test:
        [
          "CMD-SHELL",
          "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)\"",
        ]
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 10s
    networks:
      - app_bridge

  celery_worker:
    image: ${BACKEND_IMAGE}
    container_name: hm_celery_worker
    restart: unless-stopped
    environment:
      <<: *common-env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      backend:
        condition: service_healthy
    volumes:
      - *video-source-mount
      - *volume-data
    extra_hosts: *common-extra-hosts
    command: celery -A src.core.celery_app worker --loglevel=info -Q celery,analysis_hot,analysis_full
    networks:
      - app_bridge

  celery_beat:
    image: ${BACKEND_IMAGE}
    container_name: hm_celery_beat
    restart: unless-stopped
    environment:
      <<: *common-env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      backend:
        condition: service_healthy
    volumes:
      - *video-source-mount
      - *volume-data
    extra_hosts: *common-extra-hosts
    command: celery -A src.core.celery_app beat --loglevel=info
    networks:
      - app_bridge

  frontend:
    image: ${FRONTEND_IMAGE}
    container_name: hm_frontend
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
    depends_on:
      backend:
        condition: service_healthy
    ports:
      - "8102:80"
    networks:
      - app_bridge

networks:
  app_bridge:
    driver: bridge

EOF

echo "[7/7] 生成客户部署 README"
cat > "${PACKAGE_DIR}/README.md" <<EOF
# 交付部署说明

## 1. 交付内容

- images/backend_${RELEASE_TAG}.tar.gz
- images/frontend_${RELEASE_TAG}.tar.gz
- images/postgres_17-alpine.tar.gz
- images/redis_alpine.tar.gz
- images/images.txt
- images/sha256sum.txt
- docker-compose.yml

## 2. NAS 前置要求

- 已安装 Docker 与 Docker Compose
- 具备管理员权限，可执行 docker 命令
- 预留端口: 8102

## 3. 部署步骤

1) 进入交付目录并校验文件 (可选):

\`\`\`bash
cd <交付目录>
sha256sum -c images/sha256sum.txt
\`\`\`

2) 导入全部镜像:

\`\`\`bash
docker load -i images/backend_${RELEASE_TAG}.tar.gz
docker load -i images/frontend_${RELEASE_TAG}.tar.gz
docker load -i images/postgres_17-alpine.tar.gz
docker load -i images/redis_alpine.tar.gz
\`\`\`

3) 准备挂载目录 (如不存在):

\`\`\`bash
mkdir -p data xiaomi_video postgres_data redis_data
\`\`\`

4) 如需修改监控视频目录，只改 `docker-compose.yml` 顶部这一行：

\`\`\`yaml
x-video-source-mount: &video-source-mount "./xiaomi_video:/data/videos"
\`\`\`

5) 启动服务:

\`\`\`bash
docker compose up -d
\`\`\`

## 4. 访问地址

- 前端: http://<NAS_IP>:8102
- 系统健康检查(经前端反向代理): http://<NAS_IP>:8102/health

## 5. 运维命令

\`\`\`bash
docker compose ps
docker compose logs -f backend
docker compose logs -f frontend
docker compose down
\`\`\`
EOF

echo "打包完成: ${PACKAGE_DIR}"
