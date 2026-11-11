-- =============================================================================
-- smart-ERP 演示库最小开通脚本（PostgreSQL 15，无 pgvector）
-- -----------------------------------------------------------------------------
-- 用法（超级用户执行一次；口令用 psql 变量传入，不落盘）：
--   psql -h <pg-host> -U postgres -d postgres \
--        -v erp_app_pwd='<erp_app 口令>' -v erp_mig_pwd='<erp_migrator 口令>' \
--        -f demo/cloud/provision-demo.sql
--
-- 与 deploy/smart-erp/db/00-provision.sql 的差异（仅演示库口径）：
--   1. 不创建 vector 扩展——演示数据不含向量字段，也不依赖 pgvector；
--   2. 不建库、不建 schema——库与 demo/erp schema 由 import.sh 按 UTF8 创建并还原；
--   3. 只准备两个角色的登录属性与角色级超时/搜索路径。
-- 生产/共用实例仍以 deploy 侧开通脚本为准，本文件不改动 deploy/ 下任何内容。
-- =============================================================================

\set ON_ERROR_STOP on

DO $$
DECLARE
  ver int := current_setting('server_version_num')::int;
BEGIN
  IF ver < 150000 OR ver >= 160000 THEN
    RAISE EXCEPTION '需要 PostgreSQL 15.x，当前 server_version_num=%', ver;
  END IF;
END $$;

SELECT format(
  'CREATE ROLE erp_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 20',
  :'erp_app_pwd')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app')
\gexec

SELECT format(
  'CREATE ROLE erp_migrator LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 4',
  :'erp_mig_pwd')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_migrator')
\gexec

ALTER ROLE erp_app      LOGIN PASSWORD :'erp_app_pwd'      NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 20;
ALTER ROLE erp_migrator LOGIN PASSWORD :'erp_mig_pwd'      NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 4;

ALTER ROLE erp_app      SET search_path = erp;
ALTER ROLE erp_app      SET statement_timeout = '30s';
ALTER ROLE erp_app      SET lock_timeout = '3s';
ALTER ROLE erp_app      SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE erp_migrator SET search_path = erp;
ALTER ROLE erp_migrator SET lock_timeout = '3s';

SELECT current_setting('server_version') AS pg_version,
       (SELECT array_agg(rolname ORDER BY rolname) FROM pg_roles
         WHERE rolname IN ('erp_app', 'erp_migrator')) AS roles;
