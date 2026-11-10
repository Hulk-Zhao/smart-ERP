#!/usr/bin/env bash
# =============================================================================
# smart-ERP 恢复（默认拒绝写入非空库；必须显式 --force）
# -----------------------------------------------------------------------------
# 只做两件事：
#   1. 把 pg_dump -Fc 的备份 pg_restore 到**指定目标库**（默认要求库名以 smart_erp 开头）；
#   2. 可选把 erp_objects 的 tar.gz 解到指定卷。
#
# 安全边界：
#   - 目标库名必须以 smart_erp 开头，否则直接拒绝（防止把备份恢复进 APS 的库）；
#   - 目标库里只要有用户表就拒绝，除非显式 --force；--force 时用
#     pg_restore --clean --if-exists 只清备份包里的对象，不做整库 DROP；
#   - 恢复完成后只打印核对结果，**不会自动执行 alembic upgrade head**，
#     迁移必须走 deploy/smart-erp/RUNBOOK.md 的升级流程（迁移不可自动回退）。
#
# 用法：
#   scripts/restore-smart-erp.sh \
#     --dump backups/2026-11-11/smart_erp-...dump \
#     --target-url postgresql+asyncpg://erp_migrator:***@hulkai_aps_postgres:5432/smart_erp_restore \
#     [--objects backups/.../erp_objects-...tar.gz --target-volume erp_objects_restore] \
#     [--force]
#
# 退出码：0 成功；2 参数/环境不满足；3 安全红线；4 目标库非空且未加 --force；
#         5 恢复后对象集合与备份包不一致（--force 留下的旧对象，见 §3.5）。
# =============================================================================
set -euo pipefail

PG_IMAGE="${ERP_BACKUP_PG_IMAGE:-postgres:15-alpine}"
OBJ_IMAGE="${ERP_BACKUP_OBJECT_IMAGE:-alpine:3.20}"
NETWORK="${ERP_DB_NETWORK:-hulkai-gz-api_default}"
DUMP=""
TARGET_URL=""
OBJECTS_TAR=""
TARGET_VOLUME=""
FORCE=0

usage() {
  cat <<'EOF'
用法: restore-smart-erp.sh --dump <file> --target-url <url> [选项]
  --dump <file>          pg_dump -Fc 产物
  --target-url <url>     恢复目标连接串（库名必须以 smart_erp 开头）
  --objects <file>       erp_objects 的 tar.gz（可选）
  --target-volume <名>   对象解包到哪个卷（给 --objects 时必须）
  --force                目标库非空也继续（pg_restore --clean --if-exists）
  -h, --help             显示帮助

环境变量：ERP_DB_NETWORK / ERP_BACKUP_PG_IMAGE / ERP_BACKUP_OBJECT_IMAGE
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump)          DUMP="${2:-}"; shift 2 ;;
    --target-url)    TARGET_URL="${2:-}"; shift 2 ;;
    --objects)       OBJECTS_TAR="${2:-}"; shift 2 ;;
    --target-volume) TARGET_VOLUME="${2:-}"; shift 2 ;;
    --force)         FORCE=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *)               usage; die "未知参数: $1" ;;
  esac
done

[[ -f "$DUMP" ]] || die "备份文件不存在：${DUMP}"
[[ -n "$TARGET_URL" ]] || die "缺少 --target-url"
if [[ -n "$OBJECTS_TAR" && -z "$TARGET_VOLUME" ]]; then
  die "给了 --objects 就必须给 --target-volume（不会往现有卷里乱解）"
fi
command -v docker >/dev/null 2>&1 || die "需要 docker"

# postgresql+asyncpg://user:pass@host:port/db?params → 各段
_parse_url() {
  local url="$1"
  local rest="${url#*://}"
  local creds="${rest%%@*}"
  local hostpart="${rest#*@}"
  DB_USER="${creds%%:*}"
  DB_PASS=""
  [[ "$creds" == *:* ]] && DB_PASS="${creds#*:}"
  local hostport="${hostpart%%/*}"
  local dbpart="${hostpart#*/}"
  DB_NAME="${dbpart%%\?*}"
  DB_HOST="${hostport%%:*}"
  DB_PORT="${hostport#*:}"
  [[ "$DB_PORT" == "$hostport" ]] && DB_PORT=5432
  DB_PASS="${DB_PASS%%\?*}"
}
_parse_url "$TARGET_URL"

# 红线：目标库名必须以 smart_erp 开头（演练库允许 smart_erp_restore 这类后缀）
if [[ ! "$DB_NAME" =~ ^smart_erp($|_) ]]; then
  echo "ERROR: 目标库 '${DB_NAME}' 不是 smart_erp / smart_erp_*，拒绝恢复（防止写进 APS 的库）" >&2
  exit 4
fi

psql_target() {
  docker run --rm --network "$NETWORK" -e "PGPASSWORD=${DB_PASS}" "$PG_IMAGE" \
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc "$1"
}

# ---- 1. 非空库检查 ----------------------------------------------------------
EXISTING="$(psql_target "select count(*) from information_schema.tables
                        where table_schema not in ('pg_catalog','information_schema')" | tr -d '[:space:]')"
if [[ "$EXISTING" != "0" && "$FORCE" -eq 0 ]]; then
  echo "ERROR: 目标库 ${DB_NAME} 已有 ${EXISTING} 张用户表，拒绝覆盖" >&2
  psql_target "select table_schema || '.' || table_name from information_schema.tables
               where table_schema not in ('pg_catalog','information_schema') limit 10" >&2
  echo "如确认要覆盖，请加 --force（只清备份包内对象，不做整库 DROP）" >&2
  exit 4
fi

echo "== smart-ERP 恢复：${DUMP} → ${DB_NAME}（force=${FORCE}）=="

# ---- 2. 数据库恢复 ----------------------------------------------------------
RESTORE_ARGS=(--single-transaction --exit-on-error)
(( FORCE )) && RESTORE_ARGS+=(--clean --if-exists)

echo "+ pg_restore ${RESTORE_ARGS[*]}"
docker run --rm -i --network "$NETWORK" -e "PGPASSWORD=${DB_PASS}" "$PG_IMAGE" \
  pg_restore --dbname "postgresql://${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}" \
  "${RESTORE_ARGS[@]}" < "$DUMP"

# ---- 3. 核对 ----------------------------------------------------------------
TABLE_COUNT="$(psql_target "select count(*) from information_schema.tables where table_schema = 'erp'" | tr -d '[:space:]')"
ALEMBIC_REV="$(psql_target "select coalesce(max(version_num), 'NONE') from erp.alembic_version_erp" 2>/dev/null | tr -d '[:space:]' || true)"
[[ -n "$ALEMBIC_REV" ]] || ALEMBIC_REV="NONE"
echo "核对：erp schema 表数=${TABLE_COUNT}；alembic 版本=${ALEMBIC_REV}"

# ---- 3.5 一致性校验：库内对象集合 − 备份包对象集合 必须为空 -------------------
# --force 的 --clean 只清「备份包里出现过的对象」，不会删掉备份包里没有的旧表/
# 旧序列/旧外键，可能得到半新半旧的库。这里显式比对用户 schema（erp/public）下的
# 表/序列/视图/物化视图/外部表/外键，多出来的即拒绝通过。
HOST_DUMP_DIR="$(cd "$(dirname "$DUMP")" && { pwd -W 2>/dev/null || pwd; })"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

dump_objects() {
  MSYS_NO_PATHCONV=1 docker run --rm -v "${HOST_DUMP_DIR}:/dump:ro" "$PG_IMAGE" \
    pg_restore -l "/dump/$(basename "$DUMP")" | awk '
      # pg_restore -l 行格式：<dumpId>; <catalogOid> <oid> <TYPE...> <schema> <name> [<owner>]
      /^;/ { next }
      $1 !~ /^[0-9]+;$/ { next }
      {
        t=$4; s=$5; n=$6
        if (t=="TABLE" && $5=="DATA") { next }
        if (t=="TABLE" || t=="SEQUENCE" || t=="VIEW") { }
        else if (t=="MATERIALIZED" && $5=="VIEW")    { t="MATERIALIZED VIEW"; s=$6; n=$7 }
        else if (t=="FOREIGN" && $5=="TABLE")        { t="FOREIGN TABLE";   s=$6; n=$7 }
        else if (t=="FK" && $5=="CONSTRAINT")        { print "FK "$6"."$7" "$8; next }
        else { next }
        if (s=="erp" || s=="public") print t" "s"."n
      }' | sort -u
}

db_objects() {
  psql_target "
    select case c.relkind
             when 'r' then 'TABLE' when 'p' then 'TABLE'
             when 'S' then 'SEQUENCE' when 'v' then 'VIEW'
             when 'm' then 'MATERIALIZED VIEW' when 'f' then 'FOREIGN TABLE'
           end || ' ' || n.nspname || '.' || c.relname
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname not in ('pg_catalog','information_schema','pg_toast')
       and n.nspname not like 'pg_temp%' and n.nspname not like 'pg_toast_temp%'
       and c.relkind in ('r','p','S','v','m','f')
       and not exists (select 1 from pg_depend d
                        where d.classid = 'pg_class'::regclass
                          and d.objid = c.oid and d.deptype = 'e')
    union all
    select 'FK ' || n.nspname || '.' || c.relname || ' ' || con.conname
      from pg_constraint con
      join pg_class c on c.oid = con.conrelid
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname not in ('pg_catalog','information_schema','pg_toast')
       and n.nspname not like 'pg_temp%' and n.nspname not like 'pg_toast_temp%'
       and con.contype = 'f'" \
    | sed '/^$/d' | sort -u
}

dump_objects > "${TMP_DIR}/dump.txt"
db_objects   > "${TMP_DIR}/db.txt"
EXTRA="$(comm -13 "${TMP_DIR}/dump.txt" "${TMP_DIR}/db.txt")"
MISSING="$(comm -23 "${TMP_DIR}/dump.txt" "${TMP_DIR}/db.txt")"
DUMP_OBJ_COUNT="$(wc -l < "${TMP_DIR}/dump.txt" | tr -d '[:space:]')"
DB_OBJ_COUNT="$(wc -l < "${TMP_DIR}/db.txt" | tr -d '[:space:]')"
echo "一致性：备份包对象=${DUMP_OBJ_COUNT}；恢复库对象=${DB_OBJ_COUNT}"

if [[ -n "$EXTRA" ]]; then
  echo "ERROR: 恢复库存在备份包中没有的对象（--force 只清包内对象，旧对象被留下）：" >&2
  echo "$EXTRA" | sed 's/^/  + /' >&2
  echo "处理：确认这些对象可删除后手工 DROP，再用同一备份 --force 重跑；不要直接放行。" >&2
  exit 5
fi
if [[ -n "$MISSING" ]]; then
  echo "WARN: 备份包中有对象未出现在恢复库（pg_restore 未完整恢复）：" >&2
  echo "$MISSING" | sed 's/^/  - /' >&2
  exit 5
fi
echo "一致性校验通过：库内对象集合 − 备份包对象集合 = 空"

# ---- 4. 对象卷 --------------------------------------------------------------
# 只解到 --target-volume（对象卷）。宿主 inbox（docker-compose.yml 的 ./inbox:/data/inbox:ro）
# **不在恢复范围内**：恢复后待处理的原始单据原样保留，这里绝不删除/清空 inbox，
# 重复导入由业务幂等键去重，不靠删文件。
if [[ -n "$OBJECTS_TAR" ]]; then
  [[ -f "$OBJECTS_TAR" ]] || die "对象包不存在：${OBJECTS_TAR}"
  echo "+ 解包 ${OBJECTS_TAR} → 卷 ${TARGET_VOLUME}"
  docker volume inspect "$TARGET_VOLUME" >/dev/null 2>&1 || docker volume create "$TARGET_VOLUME" >/dev/null
  HOST_OBJ_DIR="$(cd "$(dirname "$OBJECTS_TAR")" && { pwd -W 2>/dev/null || pwd; })"
  MSYS_NO_PATHCONV=1 docker run --rm -v "${TARGET_VOLUME}:/data" -v "${HOST_OBJ_DIR}:/backup:ro" \
    "$OBJ_IMAGE" sh -c "cd /data && tar -xzf /backup/$(basename "$OBJECTS_TAR")"
fi

cat <<EOF
恢复完成。下一步（不要在恢复脚本里自动跑迁移）：
  1. 迁移：docker compose --profile full run --rm migrate
  2. 校验：alembic current 应与备份元信息一致（备份时会写进 .meta.txt）
  3. 再启动：docker compose --profile demo up -d
注意：迁移不可自动回退。若版本高于代码预期，必须先确认 alembic downgrade 的影响面，
     回退数据库比回退镜像危险得多（见 deploy/smart-erp/RUNBOOK.md）。
EOF
