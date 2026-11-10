#!/usr/bin/env bash
# =============================================================================
# smart-ERP 升级（旧 tag → 新 tag + 一次性迁移），失败时给出/执行回滚到旧 sha
# -----------------------------------------------------------------------------
# 顺序（对应 docs/product/tech/01-技术架构.md §1A.10 发布序列）：
#   0. 前置：备份存在、新旧镜像都在本机、compose 能解析；
#   1. migrate：单独起一次性 migrate 服务（profile full），失败即停，不碰应用容器；
#   2. 应用：按档位 up -d，等 api /healthz 200；
#   3. 失败：默认只打印回滚命令；加 --auto-rollback 则自动用旧 tag 重建应用容器。
#
# 重要（脚本会重复提示）：**迁移不可自动回退**。
#   回滚镜像不会把数据库降级；若新版本已执行破坏性迁移，
#   必须人工评估 alembic downgrade 或从备份恢复（见 deploy/smart-erp/RUNBOOK.md）。
#
# 用法：
#   scripts/upgrade-smart-erp.sh --old-tag <40位sha> --new-tag <40位sha> [--profile demo|full]
#   scripts/upgrade-smart-erp.sh ... --auto-rollback
#
# 退出码：0 成功；2 参数/前置不满足；5 迁移失败；6 健康检查失败。
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="${ERP_DEPLOY_DIR:-${REPO_ROOT}/deploy/smart-erp}"
ENV_FILE="${ERP_ENV_FILE:-${DEPLOY_DIR}/.env}"
OLD_TAG=""
NEW_TAG=""
PROFILE="demo"
HEALTH_URL="${ERP_HEALTH_URL:-http://127.0.0.1:8010/healthz}"
WAIT_SECONDS="${ERP_UPGRADE_WAIT_SECONDS:-90}"
AUTO_ROLLBACK=0
SKIP_BACKUP_CHECK=0

usage() {
  cat <<'EOF'
用法: upgrade-smart-erp.sh --old-tag <sha> --new-tag <sha> [选项]
  --old-tag <sha>       当前在跑的版本（回滚目标，必须是本机已有镜像）
  --new-tag <sha>       要升级到的版本
  --profile <档位>      demo（默认）或 full
  --auto-rollback       健康检查失败时自动用旧 tag 重建应用容器
  --skip-backup-check   跳过「当天备份已存在」检查（不推荐）
  -h, --help            显示帮助
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --old-tag)           OLD_TAG="${2:-}"; shift 2 ;;
    --new-tag)           NEW_TAG="${2:-}"; shift 2 ;;
    --profile)           PROFILE="${2:-}"; shift 2 ;;
    --auto-rollback)     AUTO_ROLLBACK=1; shift ;;
    --skip-backup-check) SKIP_BACKUP_CHECK=1; shift ;;
    -h|--help)           usage; exit 0 ;;
    *)                   usage; die "未知参数: $1" ;;
  esac
done

[[ "$OLD_TAG" =~ ^[0-9a-f]{40}$ ]] || die "--old-tag 必须是完整 40 位 sha"
[[ "$NEW_TAG" =~ ^[0-9a-f]{40}$ ]] || die "--new-tag 必须是完整 40 位 sha"
[[ "$PROFILE" == "demo" || "$PROFILE" == "full" ]] || die "--profile 只能是 demo 或 full"

cd "$DEPLOY_DIR"
[[ -f "$ENV_FILE" ]] || die "缺少环境文件：${ENV_FILE}（cp .env.example .env 后填写）"

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "${DEPLOY_DIR}/docker-compose.yml")
"${COMPOSE[@]}" --profile "$PROFILE" config --quiet

for image in app connector-gw web; do
  docker image inspect "smart-erp/${image}:${NEW_TAG}" >/dev/null 2>&1 \
    || die "本机缺少 smart-erp/${image}:${NEW_TAG}，请先 scripts/deploy-images.sh --tag ${NEW_TAG}"
done
docker image inspect "smart-erp/app:${OLD_TAG}" >/dev/null 2>&1 \
  || die "本机缺少回滚镜像 smart-erp/app:${OLD_TAG}，回滚会失败，先补齐再升级"

# ---- 0. 备份前置 ------------------------------------------------------------
if (( ! SKIP_BACKUP_CHECK )); then
  TODAY="$(date +%Y-%m-%d)"
  LATEST_BACKUP="$(ls -1 "${ERP_BACKUP_DIR:-${REPO_ROOT}/backups}/${TODAY}"/*.dump 2>/dev/null | tail -n 1 || true)"
  [[ -n "$LATEST_BACKUP" ]] || die "今天还没有备份。先跑 scripts/backup-smart-erp.sh（或显式 --skip-backup-check）"
  echo "备份确认：${LATEST_BACKUP}"
fi

echo "== 升级：${OLD_TAG:0:12} → ${NEW_TAG:0:12}（profile=${PROFILE}）=="
echo "!! 迁移不可自动回退：回滚镜像不会降级数据库。破坏性迁移必须人工评估或从备份恢复。"

# ---- 1. 迁移（一次性服务；失败不碰应用容器）--------------------------------
set +e
ERP_IMAGE_TAG="$NEW_TAG" "${COMPOSE[@]}" --profile full run --rm --no-deps migrate
MIGRATE_RC=$?
set -e
if (( MIGRATE_RC != 0 )); then
  echo "ERROR: migrate 失败（exit=${MIGRATE_RC}）。应用容器未动，仍跑在 ${OLD_TAG:0:12}。" >&2
  echo "回滚命令（数据库未变，直接回旧镜像即可）：" >&2
  echo "  ERP_IMAGE_TAG=${OLD_TAG} docker compose --profile ${PROFILE} up -d --remove-orphans" >&2
  exit 5
fi

# ---- 2. 应用 ----------------------------------------------------------------
ERP_IMAGE_TAG="$NEW_TAG" "${COMPOSE[@]}" --profile "$PROFILE" up -d --remove-orphans

# ---- 3. 健康检查 ------------------------------------------------------------
echo "+ 等待 ${HEALTH_URL}（最多 ${WAIT_SECONDS}s）"
DEADLINE=$(( $(date +%s) + WAIT_SECONDS ))
HEALTHY=0
while (( $(date +%s) < DEADLINE )); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$HEALTH_URL" || true)"
  if [[ "$code" == "200" ]]; then HEALTHY=1; break; fi
  sleep 3
done

if (( HEALTHY )); then
  echo "升级完成：api /healthz 200，新版本 ${NEW_TAG:0:12}"
  exit 0
fi

echo "ERROR: /healthz 未在 ${WAIT_SECONDS}s 内返回 200" >&2
if (( AUTO_ROLLBACK )); then
  echo "自动回滚到 ${OLD_TAG:0:12}（注意：数据库迁移不回退）" >&2
  ERP_IMAGE_TAG="$OLD_TAG" "${COMPOSE[@]}" --profile "$PROFILE" up -d --remove-orphans
else
  echo "手工回滚命令：" >&2
  echo "  ERP_IMAGE_TAG=${OLD_TAG} docker compose --env-file ${ENV_FILE} --profile ${PROFILE} up -d --remove-orphans" >&2
fi
echo "若新版本已执行破坏性迁移：不要只回镜像，先按 RUNBOOK 判断 downgrade / 从备份恢复" >&2
exit 6
