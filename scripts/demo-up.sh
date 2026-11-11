#!/usr/bin/env bash
# =============================================================================
# demo 档一键起栈：PG → provision → migrate → 灌演示数据 → api/gw/web → 等 healthy
# -----------------------------------------------------------------------------
# 边界（与 docker-compose.yml 的设计一致）：
#   1. compose 刻意不起 PG / Redis / MinIO；PG 由本脚本以官方 postgres:15 单起，
#      **不安装 pgvector**——00-provision.sql 对 vector 缺失只告警，不阻断开通；
#   2. 迁移与灌数都用 app 镜像里的 Python（3.12）执行，宿主只需要 docker；
#   3. 全部步骤幂等：重复执行 = 重灌数据 + 重建容器，不会出现「半套」。
#
# 用法：
#   scripts/demo-up.sh                      # 用 git HEAD 作为镜像 tag
#   ERP_IMAGE_TAG=<40位sha> scripts/demo-up.sh
#   scripts/demo-up.sh --skip-load          # 只起栈，不重灌数据
#   scripts/demo-up.sh --down               # 停栈（保留 PG 与数据卷）
#
# 端口默认 8010 / 8088 / 55441；被占用时先 --down 或改 ERP_API_PORT / ERP_WEB_PORT。
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DIR="${REPO_ROOT}/deploy/smart-erp"
SCENARIO="manufacturing-multi-plant"

PG_NAME="${DEMO_PG_NAME:-s8-demo-pg}"
PG_IMAGE="${DEMO_PG_IMAGE:-postgres:15}"
PG_PORT="${DEMO_PG_PORT:-55441}"
DB_NETWORK="${DEMO_DB_NETWORK:-s8-demo-db-net}"
APP_PW="${DEMO_APP_PASSWORD:-erp_app_pw}"
MIG_PW="${DEMO_MIG_PASSWORD:-erp_migrator_pw}"

SKIP_LOAD=0

info() { printf '\n== %s ==\n' "$*"; }
die()  { echo "ERROR: $*" >&2; exit 2; }

# Git Bash 会把 `-c /app/alembic.ini`、`-w /repo` 这类容器内绝对路径改写成
# `C:/Program Files/Git/...`；只对 docker 调用关掉该转换，git 仍按 POSIX 路径走。
docker_cli() { MSYS_NO_PATHCONV=1 docker "$@"; }
# 挂载源路径在 Git Bash 下需要 Windows 形式（E:\...），否则关掉转换后 docker 不认 /e/...
host_path() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-load) SKIP_LOAD=1; shift ;;
    --down)
      docker compose --project-directory "${DEMO_DIR}" --profile demo down --remove-orphans
      docker stop "${PG_NAME}" >/dev/null 2>&1 || true
      exit 0
      ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

TAG="${ERP_IMAGE_TAG:-$(git -C "${REPO_ROOT}" rev-parse HEAD)}"
[[ "${TAG}" =~ ^[0-9a-f]{40}$ ]] || die "ERP_IMAGE_TAG 必须是完整 40 位 sha，实际 '${TAG}'"
APP_IMAGE="smart-erp/app:${TAG}"

# ---- 1. .env（从 .env.demo 生成，替换口令占位符） ---------------------------
info "1/7 生成 ${DEMO_DIR}/.env"
if [[ ! -f "${DEMO_DIR}/.env" ]]; then
  sed -e "s|CHANGE_ME_APP_PASSWORD|${APP_PW}|g" \
      -e "s|CHANGE_ME_MIG_PASSWORD|${MIG_PW}|g" \
      -e "s|^ERP_IMAGE_TAG=.*|ERP_IMAGE_TAG=${TAG}|" \
      "${DEMO_DIR}/.env.demo" > "${DEMO_DIR}/.env"
  echo "已从 .env.demo 生成 .env（口令为脚本默认值，生产请自行替换）"
else
  echo ".env 已存在，保留现有取值（ERP_IMAGE_TAG 以 .env 为准）"
fi
mkdir -p "${DEMO_DIR}/config" "${DEMO_DIR}/inbox"

# ---- 2. PG 容器 + 网络 ------------------------------------------------------
info "2/7 启动 PostgreSQL 15（无 pgvector）"
if ! docker network inspect "${DB_NETWORK}" >/dev/null 2>&1; then
  docker network create "${DB_NETWORK}" >/dev/null
fi
if docker container inspect "${PG_NAME}" >/dev/null 2>&1; then
  docker start "${PG_NAME}" >/dev/null
else
  docker run -d --name "${PG_NAME}" --network "${DB_NETWORK}" \
    -e POSTGRES_PASSWORD="${MIG_PW}" \
    -p "127.0.0.1:${PG_PORT}:5432" \
    --health-cmd "pg_isready -U postgres" --health-interval 5s --health-retries 20 \
    "${PG_IMAGE}" >/dev/null
fi
for _ in $(seq 1 60); do
  if docker exec "${PG_NAME}" pg_isready -U postgres -d postgres >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "${PG_NAME}" pg_isready -U postgres -d postgres >/dev/null \
  || die "PostgreSQL 未就绪（容器 ${PG_NAME}）"

# ---- 3. 开通库/角色（vector 缺失只告警） -----------------------------------
info "3/7 执行 00-provision.sql（缺 pgvector 只告警）"
docker exec -i "${PG_NAME}" psql -q -U postgres -d postgres \
  -v ON_ERROR_STOP=1 \
  -v erp_app_pwd="${APP_PW}" -v erp_mig_pwd="${MIG_PW}" \
  -f - < "${DEMO_DIR}/db/00-provision.sql"

# ---- 4. 迁移（erp_migrator，一次性容器） -----------------------------------
info "4/7 alembic upgrade head"
docker_cli run --rm --network "${DB_NETWORK}" \
  -e ERP_APP_ENV=demo \
  -e "ERP_DATABASE_URL=postgresql+asyncpg://erp_migrator:${MIG_PW}@${PG_NAME}:5432/smart_erp" \
  --entrypoint alembic "${APP_IMAGE}" -c /app/alembic.ini upgrade head

# ---- 5. 灌演示数据（demo/load.py，需表属主=erp_migrator） ------------------
if (( SKIP_LOAD )); then
  info "5/7 跳过数据灌入（--skip-load）"
else
  info "5/7 灌入 ${SCENARIO}（demo.* + erp.* 投影）"
  docker_cli run --rm --network "${DB_NETWORK}" \
    -v "$(host_path "${REPO_ROOT}/demo"):/repo/demo:ro" -w /repo \
    --entrypoint python "${APP_IMAGE}" /repo/demo/load.py \
    --scenario "${SCENARIO}" \
    --dsn "postgresql://erp_migrator:${MIG_PW}@${PG_NAME}:5432/smart_erp"
fi

# ---- 6. 起栈 ---------------------------------------------------------------
info "6/7 docker compose --profile demo up -d"
docker compose --project-directory "${DEMO_DIR}" --profile demo up -d

# ---- 7. 等 healthy ---------------------------------------------------------
info "7/7 等待容器 healthy（api/web/gw/worker/scheduler，最多 180s）"
for _ in $(seq 1 60); do
  states="$(docker inspect -f '{{.Name}}={{.State.Health.Status}}' \
    erp_api erp_web erp_gw erp_worker erp_scheduler 2>/dev/null || true)"
  unhealthy="$(printf '%s\n' "${states}" | grep -v '=healthy' || true)"
  if [[ -z "${unhealthy}" && -n "${states}" ]]; then
    printf '%s\n' "${states}"
    break
  fi
  sleep 3
done
[[ -z "${unhealthy}" ]] || { printf '%s\n' "${states}" >&2; die "仍有容器未 healthy"; }

TOKEN="$(sed -n 's/^ERP_DEMO_SESSION_TOKEN=//p' "${DEMO_DIR}/.env")"
cat <<EOF

栈已就绪：
  web     http://localhost:${ERP_WEB_PORT:-8088}/          （本机 nginx 不带 /demo/smart-erp/ 前缀）
  api     http://localhost:${ERP_API_PORT:-8010}/healthz
  session curl -H 'X-Session-Token: ${TOKEN}' \\
          'http://localhost:${ERP_API_PORT:-8010}/v1/auth/session'
EOF
