#!/usr/bin/env bash
# =============================================================================
# smart-ERP 镜像部署（构建机执行：scp → docker load → compose up -d）
# -----------------------------------------------------------------------------
# 依据：01 §1A.10 发布序列；C12。服务器不编译、不联网拉镜像。
# 回滚：scripts/deploy-images.sh --tag <上一个 sha>（镜像仍留在服务器，无需重传）。
#
# 用法：
#   ERP_DEPLOY_HOST=129.204.108.45 ERP_DEPLOY_USER=root \
#   ERP_DEPLOY_DIR=/opt/smart-erp \
#   scripts/deploy-images.sh --tag <40位sha>
#
#   scripts/deploy-images.sh --tag <sha> --dry-run     # 只打印将执行的命令
#   scripts/deploy-images.sh --tag <sha> --no-up       # 只传镜像，不重启栈
#
# 前置：目标机 ERP_DEPLOY_DIR 下已有 docker-compose.yml 与 .env（.env 必须由人填，
#       脚本只会补拷 docker-compose.yml 与 .env.example，绝不传输 .env）。
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ERP_DIST_DIR:-${REPO_ROOT}/dist}"
COMPOSE_FILE="${REPO_ROOT}/deploy/smart-erp/docker-compose.yml"
ENV_EXAMPLE="${REPO_ROOT}/deploy/smart-erp/.env.example"
TAG="${ERP_IMAGE_TAG:-}"
HOST="${ERP_DEPLOY_HOST:-}"
USER_NAME="${ERP_DEPLOY_USER:-}"
REMOTE_DIR="${ERP_DEPLOY_DIR:-/opt/smart-erp}"
SSH_PORT="${ERP_DEPLOY_SSH_PORT:-22}"
DRY_RUN=0
DO_UP=1

usage() {
  cat <<'EOF'
用法: deploy-images.sh [选项]
  -t, --tag <sha>     完整 40 位 git sha（也可用环境变量 ERP_IMAGE_TAG）
      --host <h>      目标机（也可用 ERP_DEPLOY_HOST）
      --user <u>      SSH 用户（也可用 ERP_DEPLOY_USER）
      --dir <path>    远端部署目录，默认 /opt/smart-erp
  -p, --port <n>      SSH 端口，默认 22
      --no-up         只 docker load，不 compose up
      --dry-run       只打印命令
  -h, --help          显示帮助
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--tag)   TAG="${2:-}"; shift 2 ;;
    --host)     HOST="${2:-}"; shift 2 ;;
    --user)     USER_NAME="${2:-}"; shift 2 ;;
    --dir)      REMOTE_DIR="${2:-}"; shift 2 ;;
    -p|--port)  SSH_PORT="${2:-}"; shift 2 ;;
    --no-up)    DO_UP=0; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          usage; die "未知参数: $1" ;;
  esac
done

[[ -n "$TAG" ]] || die "缺少 ERP_IMAGE_TAG（完整 40 位 sha）"
[[ "$TAG" =~ ^[0-9a-f]{40}$ ]] || die "ERP_IMAGE_TAG='${TAG}' 不合法：必须是完整 40 位小写十六进制 sha"
[[ -n "$HOST" ]] || die "缺少目标机：--host 或 ERP_DEPLOY_HOST"
[[ -n "$USER_NAME" ]] || die "缺少 SSH 用户：--user 或 ERP_DEPLOY_USER"

TAR_NAME="erp-images-${TAG}.tar.gz"
TAR_PATH="${DIST_DIR}/${TAR_NAME}"
SHA_PATH="${DIST_DIR}/sha256sum.txt"

[[ -f "$TAR_PATH" ]] || die "找不到产物 ${TAR_PATH}（先跑 scripts/build-images.sh --tag ${TAG}）"
[[ -f "$SHA_PATH" ]] || die "找不到校验文件 ${SHA_PATH}"
[[ -f "$COMPOSE_FILE" ]] || die "找不到 ${COMPOSE_FILE}"

# 本地校验：传坏包比传慢包更贵（5Mbps 现场）
echo "== 本地校验 =="
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$DIST_DIR" && sha256sum -c sha256sum.txt )
elif command -v shasum >/dev/null 2>&1; then
  ( cd "$DIST_DIR" && shasum -a 256 -c sha256sum.txt )
else
  echo "WARN: 本机没有 sha256sum/shasum，跳过本地校验（远端仍会校验）"
fi

SSH_OPTS=(-p "${SSH_PORT}" -o BatchMode=yes -o ConnectTimeout=10)
SSH=(ssh "${SSH_OPTS[@]}" "${USER_NAME}@${HOST}")
SCP=(scp -P "${SSH_PORT}" -o BatchMode=yes -o ConnectTimeout=10)

run_remote() {
  local cmd="$1"
  if (( DRY_RUN )); then
    echo "+ ssh ${USER_NAME}@${HOST} \"${cmd}\""
  else
    "${SSH[@]}" "$cmd"
  fi
}

echo "== 1/5 建远端目录 ${REMOTE_DIR}（含 inbox 文件通道收件箱）=="
# inbox 与 docker-compose.yml 里的 ./inbox:/data/inbox:ro 对应：
#   网关以 uid=10001 只读访问，目录属主就是 10001，权限 0750 足够；
#   必须先建好：目录缺失时 docker 会以 root 自动建一个，属主不对，connector-gw 读不到就启动失败。
run_remote "mkdir -p '${REMOTE_DIR}/dist' && install -d -m 0750 -o 10001 -g 10001 '${REMOTE_DIR}/inbox'"

echo "== 2/5 传镜像包与校验文件（约 0.18–0.25G，5–7 分钟）=="
if (( DRY_RUN )); then
  echo "+ scp ${TAR_PATH} ${USER_NAME}@${HOST}:${REMOTE_DIR}/dist/"
  echo "+ scp ${SHA_PATH} ${USER_NAME}@${HOST}:${REMOTE_DIR}/dist/"
else
  "${SCP[@]}" "$TAR_PATH" "$SHA_PATH" "${USER_NAME}@${HOST}:${REMOTE_DIR}/dist/"
fi

echo "== 3/5 传 compose 与 .env.example（不动远端已有的 .env）=="
if (( DRY_RUN )); then
  echo "+ scp ${COMPOSE_FILE} ${ENV_EXAMPLE} ${USER_NAME}@${HOST}:${REMOTE_DIR}/"
else
  "${SCP[@]}" "$COMPOSE_FILE" "$ENV_EXAMPLE" "${USER_NAME}@${HOST}:${REMOTE_DIR}/"
fi

echo "== 4/5 远端校验并 docker load =="
run_remote "set -e; cd '${REMOTE_DIR}/dist' && sha256sum -c sha256sum.txt && docker load -i '${TAR_NAME}'"
run_remote "test -f '${REMOTE_DIR}/.env' || { echo 'ERROR: ${REMOTE_DIR}/.env 不存在，先按 .env.example 填写（含 ERP_IMAGE_TAG 与 ERP_DATABASE_URL）'; exit 3; }"

if (( ! DO_UP )); then
  echo "== 5/5 已跳过 compose up（--no-up）=="
  exit 0
fi

echo "== 5/5 compose up -d（ERP_IMAGE_TAG=${TAG}）=="
run_remote "cd '${REMOTE_DIR}' && ERP_IMAGE_TAG='${TAG}' docker compose --profile demo up -d --remove-orphans"

echo "== 就绪探测 =="
# C13 契约：/healthz 必须 200；/readyz 是「SELECT 1 + 对象存储可写」。
# readyz 尚未实现时（S0 早期）只告警不阻断，避免把未实现当故障；返回 503 必须失败。
run_remote "set -e; \
  for i in \$(seq 1 30); do curl -fsS http://127.0.0.1:8010/healthz >/dev/null && break; sleep 2; \
    if [ \"\$i\" = 30 ]; then echo 'ERROR: /healthz 未在 60s 内 200'; exit 4; fi; done; \
  code=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/readyz); \
  case \"\$code\" in \
    200) echo 'readyz: 200';; \
    404) echo 'WARN: /readyz 尚未实现（C13），本次只校验了 /healthz';; \
    *) echo \"ERROR: /readyz 返回 \$code\"; exit 5;; \
  esac"

echo
echo "完成：tag=${TAG} → ${HOST}:${REMOTE_DIR}"
echo "回滚：scripts/deploy-images.sh --tag <上一个 sha>（或直接在服务器上 ERP_IMAGE_TAG=<上一 sha> docker compose up -d）"
