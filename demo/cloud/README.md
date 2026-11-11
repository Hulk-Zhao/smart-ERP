# 云服务器导入说明（PostgreSQL 15 / UTF-8 / Asia/Shanghai）

本目录提供两件东西：

- `import.sh`：一条命令把导出的 `.dump` 灌进目标库；**先清后灌、可重复执行**；
- `provision-demo.sql`：演示库最小角色开通，**不安装 pgvector**。

导入目标是 compose 里 `ERP_DATABASE_URL` 指向的同一个库（默认 `smart_erp`）。不需要改
`deploy/` 下任何文件，也不依赖导出机的路径：`.dump` 里只有 `demo` 与 `erp` 两个 schema 的
结构与数据。

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| 数据库 | PostgreSQL 15.x（导入脚本会校验 `server_version_num = 15*`） |
| 字符集 | 数据库 UTF-8（脚本按 `template0` + `ENCODING 'UTF8'` 建库） |
| 时区 | 库级 `Asia/Shanghai`（脚本会设置，失败只告警） |
| 扩展 | 不使用 pgvector；`pg_trgm` / `pgcrypto` 若需要会在导入后按需补建 |
| 客户端 | `psql`、`pg_restore`，版本 ≥ 导出端（当前导出端 15.19） |
| 权限 | `PGUSER` 需能建库/建 schema/授权（建议 `erp_migrator` 或实例超级用户） |

## 2. 准备角色（超级用户执行一次）

```bash
psql -h <pg-host> -U postgres -d postgres \
     -v erp_app_pwd='<erp_app 口令>' -v erp_mig_pwd='<erp_migrator 口令>' \
     -f demo/cloud/provision-demo.sql
```

已用 `deploy/smart-erp/db/00-provision.sql` 开通过角色时，这一步可跳过——`import.sh` 检测到
`erp_app` 存在就会直接补授权。

## 3. 导入（一条命令）

```bash
PGDATABASE=smart_erp PGHOST=<pg-host> PGPORT=5432 \
PGUSER=erp_migrator PGPASSWORD='<口令>' \
  bash demo/cloud/import.sh smart-erp-demo-YYYYMMDD-HHMM.dump
```

先预演可以加 `--dry-run`（只打印将执行的步骤，不写库）：

```bash
PGDATABASE=smart_erp PGHOST=<pg-host> PGUSER=erp_migrator \
  bash demo/cloud/import.sh --dry-run smart-erp-demo-YYYYMMDD-HHMM.dump
```

若云服务器上没有 PG 客户端，可在 PG 容器里执行（只挂载 dump 所在目录，无绝对路径依赖）：

```bash
docker run --rm -i --network host \
  -e PGDATABASE=smart_erp -e PGHOST=<pg-host> -e PGUSER=erp_migrator -e PGPASSWORD='<口令>' \
  -v "$PWD":/work -w /work postgres:15 \
  bash demo/cloud/import.sh smart-erp-demo-YYYYMMDD-HHMM.dump
```

## 4. 脚本做了什么

1. 校验服务端为 PG 15，探活；
2. 目标库不存在则按 UTF-8 创建，并把库级时区设为 `Asia/Shanghai`；
3. `DROP SCHEMA demo CASCADE; DROP SCHEMA erp CASCADE;`（保留库本身，只重建两个 schema）；
4. `pg_restore --no-owner --no-privileges --exit-on-error` 整份还原；
5. `erp_app` 存在时补 schema/表/序列/函数授权；按需补建 `pg_trgm`、`pgcrypto`（**不装 vector**）；
6. 打印三套场景的订单行数与 `erp` 投影总量，便于当场核对。

> 第 3 步是"重灌"语义：重复执行结果一致；但会清掉这两个 schema 里人工追加的数据，
> 生产库请勿使用。

## 5. 导入后校验

```sql
-- 三套场景都在
SELECT scenario, count(*) FROM demo.sales_order_line GROUP BY 1 ORDER BY 1;
-- 期望：cross-border-multicurrency 2202 / fmcg-distribution 2081 / manufacturing-multi-plant 2337

-- erp 投影
SELECT (SELECT count(*) FROM erp.recon_batch)       AS batches,   -- 9
       (SELECT count(*) FROM erp.recon_line)        AS lines,     -- 936
       (SELECT count(*) FROM erp.ticket)            AS tickets,   -- 48
       (SELECT count(*) FROM erp.audit_image)       AS images,    -- 54
       (SELECT count(*) FROM erp.approval_record)   AS approvals; -- 75

-- 迁移版本（dump 自带 head）
SELECT version_num FROM erp.alembic_version_erp;
```

## 6. 应用侧环境变量

导入后只需确认 compose 的运行环境（不改 `deploy/` 文件，写在部署机的 `.env`）：

```dotenv
ERP_DATABASE_URL=postgresql+asyncpg://erp_app:<口令>@<pg-host>:5432/smart_erp
ERP_METERING_UNIT_PRICE=0.20
ERP_METERING_CURRENCY=CNY
ERP_METERING_UNIT_LABEL=行
TZ=Asia/Shanghai
```

`ERP_METERING_*` 与 `deploy/smart-erp/env/demo.env` 保持一致；批次里也写了单价快照，
两者不一致时以批次快照为准。

## 7. 版本与回退

- dump 由 `pg_dump 15.19` 生成，`pg_restore` 版本不能低于导出端；
- dump 内 `erp.alembic_version_erp` 为 `0010_ticket_api_persistence`。若云上代码已包含更新的
  迁移，请先导入、再执行一次 `alembic upgrade head`（导入会覆盖 `erp` schema）；
- 回退：重新导入上一版 `.dump` 即可；需要保留现场时先 `pg_dump -Fc -n demo -n erp` 备份。

## 8. 需要集成侧配合的事项

1. 演示机若复用 `deploy/smart-erp/db/00-provision.sql`，会安装 pgvector；本目录的
   `provision-demo.sql` 是无 vector 版本，请决定 demo 档是否改用后者。
2. 前端每个场景对应一个 `tenant_id`，需要会话签发入口把用户映射到对应 tenant
   （当前仓库没有登录换 token 的入口，见 `demo/README.md` 缺口①）。
3. 审计影像 `/v1/audit/*` 仍是内存实现，`erp.audit_image` 的数据不会自动出现在页面。
