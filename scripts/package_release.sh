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
- 预留端口: 8226

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

- 前端: http://<NAS_IP>:8226
- 系统健康检查(经前端反向代理): http://<NAS_IP>:8226/health

## 5. 运维命令

\`\`\`bash
docker compose ps
docker compose logs -f backend
docker compose logs -f frontend
docker compose down
\`\`\`
EOF

echo "打包完成: ${PACKAGE_DIR}"
