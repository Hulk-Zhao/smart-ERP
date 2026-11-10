-- =============================================================================
-- smart_erp 共用实例开通脚本（PostgreSQL 15）— 只提交、不自动执行
-- -----------------------------------------------------------------------------
-- 依据：docs/product/tech/01-技术架构.md §1A.7.1（八项隔离）/ §1A.7.4（红线）
--       docs/product/tech/02-并发开发路线.md §4.8（共用实例变更审批）
--
-- 谁执行：**共用 PG15 实例 owner**（需要 SUPERUSER 建 vector 扩展），
--         在 §4.8 的维护窗口（周二/周四 21:00–22:00）内执行一次；
--         本项目任何容器、任何迁移都不执行本文件。
--
-- 执行方式（口令不要写进 shell history 以外的文件，建议交互输入）：
--   psql -h <pg-host> -U postgres -d postgres \
--        -v erp_app_pwd='<erp_app 口令>' \
--        -v erp_mig_pwd='<erp_migrator 口令>' \
--        -f deploy/smart-erp/db/00-provision.sql
--
-- 本脚本的边界（红线）：
--   1. 只创建/授予 smart_erp 库、erp schema、erp_app / erp_migrator 两个角色；
--   2. 只收回 **erp_app 自己** 对 public schema 的权限，**不动** PUBLIC 的默认权限
--      （否则会波及 APS / mfg 等其它住户）；
--   3. 不 DROP 任何对象，不改任何非本项目对象，不 ALTER SYSTEM；
--   4. 可重复执行（除 CREATE DATABASE 外均为幂等语句；已存在时走 \gexec 跳过）。
-- =============================================================================

\set ON_ERROR_STOP on

-- -----------------------------------------------------------------------------
-- 0. 前置校验：必须是 PG15（与共用实例口径一致，禁止 16 独有语法）
-- -----------------------------------------------------------------------------
DO $$
DECLARE
  ver int := current_setting('server_version_num')::int;
BEGIN
  IF ver < 150000 OR ver >= 160000 THEN
    RAISE EXCEPTION '需要 PostgreSQL 15.x，当前 server_version_num=%', ver;
  END IF;
  RAISE NOTICE 'PG15 校验通过（%），开始开通 smart_erp', current_setting('server_version');
END $$;

-- -----------------------------------------------------------------------------
-- 1. 角色（应用与迁移分离；应用无建库建角色权限，连接数上限 20）
-- -----------------------------------------------------------------------------
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

-- 重复执行时同步口令与属性（角色已存在时上面的 CREATE 被跳过）
ALTER ROLE erp_app      LOGIN PASSWORD :'erp_app_pwd' NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 20;
ALTER ROLE erp_migrator LOGIN PASSWORD :'erp_mig_pwd' NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 4;

-- -----------------------------------------------------------------------------
-- 2. 独立 database（跨库查询在 PG 里写不出来 → 天然隔离）
-- -----------------------------------------------------------------------------
SELECT 'CREATE DATABASE smart_erp OWNER erp_migrator ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'smart_erp')
\gexec

-- 可选加固（默认不启用，需实例 owner 判断是否有其它只读住户）：
-- REVOKE CONNECT ON DATABASE smart_erp FROM PUBLIC;

-- -----------------------------------------------------------------------------
-- 3. 切到 smart_erp 库
-- -----------------------------------------------------------------------------
\connect smart_erp

-- -----------------------------------------------------------------------------
-- 4. schema：全部业务对象只在 erp，public 不落任何表
-- -----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS erp AUTHORIZATION erp_migrator;

-- -----------------------------------------------------------------------------
-- 5. 扩展：统一建在 erp schema，避免升级时与别的项目打架
--    pg_trgm / pgcrypto 是 trusted，库 owner 可建；
--    vector 非 trusted，必须由 SUPERUSER（实例 owner）执行。
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector   SCHEMA erp;
CREATE EXTENSION IF NOT EXISTS pg_trgm  SCHEMA erp;
CREATE EXTENSION IF NOT EXISTS pgcrypto SCHEMA erp;

-- -----------------------------------------------------------------------------
-- 6. 角色级设置：search_path 兜底 + 超时（我们的慢查询不能拖住 APS 的连接与锁）
-- -----------------------------------------------------------------------------
ALTER ROLE erp_app      SET search_path = erp;
ALTER ROLE erp_app      SET statement_timeout = '30s';
ALTER ROLE erp_app      SET lock_timeout = '3s';
ALTER ROLE erp_app      SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE erp_migrator SET search_path = erp;
ALTER ROLE erp_migrator SET lock_timeout = '3s';

-- -----------------------------------------------------------------------------
-- 7. 权限
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA erp TO erp_app;

-- 只收回「我们自己」对 public 的权限；**禁止** REVOKE ... FROM PUBLIC（会波及 APS）
REVOKE ALL ON SCHEMA public FROM erp_app;

-- 后续 erp_migrator 建的表/序列/函数，自动授予 erp_app（无需每次手工 GRANT）
ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA erp
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO erp_app;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA erp
  GRANT USAGE, SELECT ON SEQUENCES TO erp_app;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA erp
  GRANT EXECUTE ON FUNCTIONS TO erp_app;

-- 应用角色没有 DDL / TRUNCATE 权限（显式收回，避免继承或误授）
REVOKE CREATE ON SCHEMA erp FROM erp_app;
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA erp FROM erp_app;

-- -----------------------------------------------------------------------------
-- 8. 版本表：独立 alembic_version_erp，不与 APS 的迁移表冲突（C14）
--    Alembic：version_table='alembic_version_erp' + version_table_schema='erp'
--             + include_schemas=False
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp.alembic_version_erp (
  version_num varchar(32) NOT NULL,
  CONSTRAINT alembic_version_erp_pkc PRIMARY KEY (version_num)
);
ALTER TABLE erp.alembic_version_erp OWNER TO erp_migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON erp.alembic_version_erp TO erp_app;

COMMENT ON TABLE erp.alembic_version_erp IS 'smart_erp 专用迁移版本表，禁止其它项目复用';

-- -----------------------------------------------------------------------------
-- 9. 审计表的写权限收紧（在 S0/S4 的迁移建出审计表之后、由实例 owner 或
--    erp_migrator 在同一窗口追加执行；此处只给模板，不预设表名存在）
-- -----------------------------------------------------------------------------
-- REVOKE UPDATE, DELETE, TRUNCATE ON erp.audit_image       FROM erp_app;
-- REVOKE UPDATE, DELETE, TRUNCATE ON erp.audit_event       FROM erp_app;
-- REVOKE UPDATE, DELETE, TRUNCATE ON erp.audit_anchor      FROM erp_app;

-- -----------------------------------------------------------------------------
-- 10. 收尾自检：把关键状态打印出来，供实例 owner 记录到变更单
-- -----------------------------------------------------------------------------
SELECT current_database()                                  AS database,
       (SELECT array_agg(nspname ORDER BY nspname)
          FROM pg_namespace
         WHERE nspname IN ('erp', 'public'))               AS schemas,
       (SELECT array_agg(extname || '@' || n.nspname ORDER BY extname)
          FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
         WHERE extname IN ('vector', 'pg_trgm', 'pgcrypto')) AS extensions,
       (SELECT rolconnlimit FROM pg_roles WHERE rolname = 'erp_app') AS erp_app_conn_limit,
       (SELECT array_agg(cfg ORDER BY cfg)
          FROM pg_db_role_setting s
          CROSS JOIN LATERAL unnest(s.setconfig) AS cfg
         WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = 'erp_app')) AS erp_app_settings;

-- 交付给 CI / 运维的字段：database=smart_erp · schema=erp · role=erp_app
-- 若 extensions 里缺 vector，preflight 会报 "missing extension vector" 而不是随机 500。
