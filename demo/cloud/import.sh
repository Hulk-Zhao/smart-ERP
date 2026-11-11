#!/usr/bin/env bash
# =============================================================================
# smart-ERP demo 数据导入（云服务器侧执行，PostgreSQL 15）
# -----------------------------------------------------------------------------
# 用法（一条命令）：
#   PGDATABASE=smart_erp PGHOST=<pg-host> PGPORT=5432 PGUSER=erp_migrator PGPASSWORD=<pwd> \
#     bash demo/cloud/import.sh smart-erp-demo-20261111-1200.dump
#
# 语义：
#   1. 目标库不存在时按 UTF8 + template0 创建，并把库级时区设为 Asia/Shanghai；
#   2. 重建 demo / erp 两个 schema（重复执行结果一致，等价于「先清后灌」）；
#   3. 用 pg_restore 整份还原（--no-owner --no-privileges，不依赖导出机路径与属主）；
#   4. 角色 erp_app 存在时补授权；不存在则跳过并提示先跑 provision-demo.sql；
#   5. 打印每个场景的行数与 erp 投影的总量，便于当场核对。
#
# 依赖：psql / pg_restore（PG 15 客户端即可，版本需 >= 导出端）。
# 不依赖：pgvector、任何绝对路径、本机目录结构。
# =============================================================================
set -euo pipefail

DUMP="${1:-}"
DRY_RUN=0
if [[ "${DUMP}" == "--dry-run" ]]; then
  DRY_RUN=1
  DUMP="${2:-}"
fi

if [[ -z "${DUMP}" || ! -f "${DUMP}" ]]; then
  echo "用法: PGDATABASE=... PGUSER=... bash demo/cloud/import.sh [--dry-run] <dump 文件>" >&2
  exit 2
fi

DB="${PGDATABASE:-smart_erp}"
export PGDATABASE="${DB}"
export TZ=Asia/Shanghai
export PGCLIENTENCODING=UTF8

if [[ "${DB}" == *'"'* ]]; then
  echo "ERROR: 数据库名不能包含双引号" >&2
  exit 2
fi

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

psql_admin() { run psql -X -q -v ON_ERROR_STOP=1 -d postgres -c "$1"; }
psql_db()    { run psql -X -q -v ON_ERROR_STOP=1 -d "${DB}" -c "$1"; }
query_db()   { psql -X -tA -d "${DB}" -c "$1"; }

echo "[1/5] 连接探活与版本校验"
SERVER_VERSION="$(psql -X -tA -d postgres -c "SHOW server_version_num")"
SERVER_VERSION="${SERVER_VERSION//[[:space:]]/}"
if [[ "${SERVER_VERSION}" != 15* ]]; then
  echo "ERROR: 需要 PostgreSQL 15.x，当前 server_version_num=${SERVER_VERSION}" >&2
  exit 2
fi
echo "      PG ${SERVER_VERSION}"

echo "[2/5] 目标库 ${DB}（UTF8 / Asia/Shanghai）"
EXISTS="$(psql -X -tA -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '${DB}'")"
if [[ "${EXISTS}" != "1" ]]; then
  run createdb -E UTF8 -T template0 "${DB}"
  echo "      已创建"
else
  echo "      已存在，保留库级设置"
fi
# 需要库 owner/超级用户；失败只告警，不阻断导入
run psql -X -q -d postgres -c "ALTER DATABASE \"${DB}\" SET timezone TO 'Asia/Shanghai'" \
  || echo "      WARN: 未能设置库级时区（请让 DBA 执行 ALTER DATABASE ${DB} SET timezone TO 'Asia/Shanghai'）"

echo "[3/5] 重建 demo / erp schema（先清后灌）"
psql_db "DROP SCHEMA IF EXISTS demo CASCADE; DROP SCHEMA IF EXISTS erp CASCADE;"

echo "[4/5] pg_restore 还原 ${DUMP}"
run pg_restore --no-owner --no-privileges --exit-on-error -d "${DB}" "${DUMP}"

echo "[5/5] 权限与自检"
if [[ "${DRY_RUN}" -eq 0 ]]; then
  ROLE_EXISTS="$(psql -X -tA -d postgres -c "SELECT 1 FROM pg_roles WHERE rolname = 'erp_app'")"
  if [[ "${ROLE_EXISTS}" == "1" ]]; then
    psql_db "GRANT USAGE ON SCHEMA erp, demo TO erp_app;
             GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA erp TO erp_app;
             GRANT SELECT ON ALL TABLES IN SCHEMA demo TO erp_app;
             GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA erp TO erp_app;
             GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA erp TO erp_app;
             ALTER DEFAULT PRIVILEGES IN SCHEMA erp
               GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO erp_app;
             ALTER DEFAULT PRIVILEGES IN SCHEMA erp
               GRANT USAGE, SELECT ON SEQUENCES TO erp_app;"
    # trusted 扩展：按需补建，失败不阻断；演示环境明确不安装 pgvector
    psql -X -q -d "${DB}" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA erp" 2>/dev/null || true
    psql -X -q -d "${DB}" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto SCHEMA erp" 2>/dev/null || true
    echo "      已为 erp_app 补授权"
  else
    echo "      WARN: 角色 erp_app 不存在，跳过授权；请先执行 demo/cloud/provision-demo.sql"
  fi

  echo "      —— 场景行数（demo.sales_order_line）——"
  query_db "SELECT scenario || ': ' || cnt FROM (
              SELECT scenario, count(*) AS cnt FROM demo.sales_order_line
              GROUP BY scenario) t ORDER BY scenario" | sed 's/^/      /'
  echo "      —— erp 投影 ——"
  query_db "SELECT 'recon_batch=' || (SELECT count(*) FROM erp.recon_batch)
                || ', recon_line=' || (SELECT count(*) FROM erp.recon_line)
                || ', ticket=' || (SELECT count(*) FROM erp.ticket)
                || ', audit_image=' || (SELECT count(*) FROM erp.audit_image)
                || ', approval_record=' || (SELECT count(*) FROM erp.approval_record)" | sed 's/^/      /'
fi

echo "[done] demo 数据导入完成：${DB}"
