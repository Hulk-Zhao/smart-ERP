#!/usr/bin/env bash
# =============================================================================
# smart-ERP 备份（只备份本项目的对象，绝不碰 APS）
# -----------------------------------------------------------------------------
# 覆盖范围（与 docs/product/tech/01-技术架构.md §1A.7.1 的隔离边界一致）：
#   1. 数据库：共用 PG15 实例上的 **smart_erp 一个库**（含 erp schema 与
#      erp.alembic_version_erp 迁移版本表），pg_dump -Fc 单库导出；
#   2. 对象存储：erp_objects 卷（LocalObjectStore 落盘对象 + sha256 清单）整卷 tar.gz。
#
# 明确不备份：
#   - 文件通道收件箱（inbox）：宿主绑定挂载的瞬时输入（供应商对账单/送货单），
#     不落在 erp_objects 卷里；打包时仍显式 --exclude，防止有人把它拷进卷后带进备份；
#   - APS / mfg 等其它住户的库、schema、表、序列一律不碰；
#   - 不 dump pg_database / pg_roles 等实例级对象（那是实例 owner 的备份职责）；
#   - 不在 SQL 里做全实例扫描，避免把别人的数据带进我们的备份包。
#
# 产物（默认 ./backups/<yyyy-mm-dd>/）：
#   smart_erp-<时间戳>-<ERP_IMAGE_TAG 前 12 位>.dump
#   erp_objects-<时间戳>-<ERP_IMAGE_TAG 前 12 位>.tar.gz
#   smart_erp-<时间戳>-<...>.meta.txt     # 版本、表数、体积，供恢复比对
#   SHA256SUMS                            # 两个产物的 sha256（追加写）
#
# 用法：
#   ERP_IMAGE_TAG=$(git rev-parse HEAD) ERP_BACKUP_DATABASE_URL=... scripts/backup-smart-erp.sh
#   scripts/backup-smart-erp.sh --out /data/backups --skip-objects
#
# 退出码：0 成功；2 参数/前置条件不满足；3 安全红线（库名不是 smart_erp）。
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_IMAGE="${ERP_BACKUP_PG_IMAGE:-postgres:15-alpine}"
OBJ_IMAGE="${ERP_BACKUP_OBJECT_IMAGE:-alpine:3.20}"
OUT_ROOT="${ERP_BACKUP_DIR:-${REPO_ROOT}/backups}"
NETWORK="${ERP_DB_NETWORK:-hulkai-gz-api_default}"
OBJECTS_VOLUME="${ERP_OBJECTS_VOLUME:-smart-erp_objects}"
SKIP_OBJECTS=0
KEEP_DAYS=""   # 只提示，不自动删（删备份必须是人工动作）

usage() {
  cat <<'EOF'
用法: backup-smart-erp.sh [选项]
  --out <dir>        备份产物根目录（默认 <repo>/backups）
  --skip-objects     只备份数据库，不打包 erp_objects 卷
  --keep-days <n>    仅打印保留策略提示（不自动删除）
  -h, --help         显示帮助

必需环境变量：
  ERP_IMAGE_TAG           当前发布 tag（完整 40 位 sha），用于产物命名与追溯
  ERP_BACKUP_DATABASE_URL 备份用连接串（建议 erp_migrator；缺省回退 ERP_MIGRATION_DATABASE_URL）

可选环境变量：
  ERP_DB_NETWORK          PG 所在 docker 网络（默认 hulkai-gz-api_default）
  ERP_OBJECTS_VOLUME      对象存储卷名（默认 smart-erp_objects，与 compose volumes 一致）
  ERP_BACKUP_PG_IMAGE     带 pg_dump 的镜像（默认 postgres:15-alpine）
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)         OUT_ROOT="${2:-}"; shift 2 ;;
    --skip-objects) SKIP_OBJECTS=1; shift ;;
    --keep-days)   KEEP_DAYS="${2:-}"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *)             usage; die "未知参数: $1" ;;
  esac
done

TAG="${ERP_IMAGE_TAG:-}"
[[ "$TAG" =~ ^[0-9a-f]{40}$ ]] || die "ERP_IMAGE_TAG 必须是完整 40 位 sha（当前: '${TAG}'）"

URL="${ERP_BACKUP_DATABASE_URL:-${ERP_MIGRATION_DATABASE_URL:-}}"
[[ -n "$URL" ]] || die "缺少 ERP_BACKUP_DATABASE_URL（或 ERP_MIGRATION_DATABASE_URL）"

# postgresql+asyncpg://user:pass@host:port/db?params → 各段
_parse_url() {
  local url="$1"
  local rest="${url#*://}"
  CREDS="${rest%%@*}"
  local hostpart="${rest#*@}"
  DB_USER="${CREDS%%:*}"
  DB_PASS=""
  [[ "$CREDS" == *:* ]] && DB_PASS="${CREDS#*:}"
  local hostport="${hostpart%%/*}"
  local dbpart="${hostpart#*/}"
  DB_NAME="${dbpart%%\?*}"
  DB_HOST="${hostport%%:*}"
  DB_PORT="${hostport#*:}"
  [[ "$DB_PORT" == "$hostport" ]] && DB_PORT=5432
  DB_PASS="${DB_PASS%%\?*}"
}
_parse_url "$URL"

# 红线：只允许 smart_erp 这一个库；库名不符直接拒绝（防止把 APS 备进来）
if [[ "$DB_NAME" != "smart_erp" ]]; then
  echo "ERROR: 目标库是 '${DB_NAME}'，本脚本只允许备份 smart_erp；APS 等其它住户不在本备份范围" >&2
  exit 3
fi

command -v docker >/dev/null 2>&1 || die "需要 docker"

STAMP="$(date +%Y-%m-%dT%H%M%S)"
DAY="$(date +%Y-%m-%d)"
SHORT="${TAG:0:12}"
OUT_DIR="${OUT_ROOT}/${DAY}"
mkdir -p "$OUT_DIR"

DUMP_NAME="smart_erp-${STAMP}-${SHORT}.dump"
DUMP_PATH="${OUT_DIR}/${DUMP_NAME}"
META_PATH="${OUT_DIR}/smart_erp-${STAMP}-${SHORT}.meta.txt"

echo "== smart-ERP 备份：db=${DB_NAME} tag=${SHORT} =="
echo "边界：只备份 ${DB_NAME} 库 + ${OBJECTS_VOLUME} 卷；不备份 APS 的任何对象"

# ---- 1. 数据库（单库、自定义格式，便于 pg_restore 选择性恢复）----------------
# 保留 owner 与 ACL：恢复到同实例的新库时权限原样回来（erp_app 只读/读写边界不丢）
echo "+ pg_dump -Fc ${DB_NAME} → ${DUMP_PATH}"
docker run --rm --network "$NETWORK" -e "PGPASSWORD=${DB_PASS}" "$PG_IMAGE" \
  pg_dump -Fc -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
  > "$DUMP_PATH"

# ---- 2. 元信息（恢复后用于比对表数量与迁移版本）------------------------------
TABLE_COUNT="$(docker run --rm --network "$NETWORK" -e "PGPASSWORD=${DB_PASS}" "$PG_IMAGE" \
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc \
  "select count(*) from information_schema.tables where table_schema = 'erp'" | tr -d '[:space:]')"
ALEMBIC_REV="$(docker run --rm --network "$NETWORK" -e "PGPASSWORD=${DB_PASS}" "$PG_IMAGE" \
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc \
  "select coalesce(max(version_num), 'NONE') from erp.alembic_version_erp" | tr -d '[:space:]')"

{
  echo "backup_file=${DUMP_NAME}"
  echo "created_at=${STAMP}"
  echo "image_tag=${TAG}"
  echo "database=${DB_NAME}"
  echo "schema=erp"
  echo "erp_table_count=${TABLE_COUNT}"
  echo "alembic_revision=${ALEMBIC_REV}"
  echo "objects_volume=${OBJECTS_VOLUME}"
  echo "excluded=APS/mfg 等其它住户的库与对象；实例级对象（pg_database/pg_roles）；inbox 瞬时收件目录"
} > "$META_PATH"

# ---- 3. 对象卷 --------------------------------------------------------------
if (( SKIP_OBJECTS )); then
  echo "+ 跳过对象卷（--skip-objects）"
else
  OBJ_NAME="erp_objects-${STAMP}-${SHORT}.tar.gz"
  HOST_OUT_DIR="$(cd "$OUT_DIR" && { pwd -W 2>/dev/null || pwd; })"
  echo "+ tar ${OBJECTS_VOLUME} → ${OUT_DIR}/${OBJ_NAME}（排除 inbox）"
  # --exclude inbox：inbox 是宿主的瞬时收件目录（绑定挂载，见 docker-compose.yml
  # 的 ./inbox:/data/inbox:ro），本不该出现在对象卷里；但万一有人 cp 进去，
  # 备份也不能把它带走——原始单据会因此被复制到备份机。两种写法都排掉，兼容 busybox tar。
  MSYS_NO_PATHCONV=1 docker run --rm \
    -v "${OBJECTS_VOLUME}:/data:ro" -v "${HOST_OUT_DIR}:/backup" \
    "$OBJ_IMAGE" sh -c "cd /data && tar -czf /backup/${OBJ_NAME} --exclude='./inbox' --exclude='inbox' ."
fi

# ---- 4. 校验和 --------------------------------------------------------------
# 只写文件名（不带目录），`sha256sum -c SHA256SUMS` 在产物目录内可直接校验
echo "+ sha256"
SUMS=("$DUMP_NAME" "$(basename "$META_PATH")")
if [[ -n "${OBJ_NAME:-}" ]]; then SUMS+=("$OBJ_NAME"); fi
( cd "$OUT_DIR" && sha256sum "${SUMS[@]}" >> SHA256SUMS )
echo "产物目录：${OUT_DIR}"
echo "启用 --keep-days 提示：${KEEP_DAYS:-未指定}"
echo "提醒：备份不是「拷出来就算完」，恢复演练按 deploy/smart-erp/RUNBOOK.md 每月做一次"
