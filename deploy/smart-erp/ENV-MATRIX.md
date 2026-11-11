# 三套环境配置矩阵（T803）

本文件记录 `ERP_*` 在 **本地 / 演示机 / 生产** 三套环境下的真实取值，配套：

- 模板（唯一真源，C11）：[`../.env.example`](../.env.example)
- 机器可校验的 schema：[`contracts/config.schema.json`](../../../contracts/config.schema.json)
- 校验脚本：[`scripts/validate_config_schema.py`](../../../scripts/validate_config_schema.py)（CI 的 `deploy-static` job 会跑它）
- 取值文件：[`env/local.env`](./env/local.env)、[`env/demo.env`](./env/demo.env)、[`env/prod.env`](./env/prod.env)

## 1. 三套环境与档位

| 环境 | compose 档位 | `ERP_APP_ENV` | 8010 绑定 | 对象存储 | 镜像 tag |
|---|---|---|---|---|---|
| 本地 local | `--profile demo` | `local` | `127.0.0.1` | local | `dev` |
| 演示机 demo | `--profile demo` | `demo` | `0.0.0.0`（安全组必须限源） | local | 40 位 sha |
| 生产 prod | `--profile full` | `full` | `127.0.0.1`（只经香港 nginx） | minio（WORM） | 40 位 sha |

档位口径只有 `demo` / `full` 两个取值，**不存在 ops 档**（T802 定稿）：

- `--profile` 只接受 `demo`、`full`；取其它值 compose 会直接报 no such profile；
- `demo` = api / worker / scheduler / connector-gw / web（1200MiB）+ u8-stub（64MiB，模拟外部系统，演示写入用）；
- `full` = 在 demo 基础上加 doc / worker-agent / worker-io / migrate，api 改为 `api-prod` 且只绑回环；
- 代码侧 `ERP_APP_ENV` 是自由字符串（无枚举校验），上表三个值是本项目约定值，`local` 仅本地用。

## 2. 取值矩阵

`—（用默认值）` 表示该环境不显式设置该键，由 `.env.example` / compose / 代码里的默认值生效。
`CHANGE_ME` 表示真实值由密管/部署时注入，仓库内不留明文。

| 键 | local | demo | prod |
|---|---|---|---|
| `ERP_IMAGE_TAG` | dev | e1926cf6748fe0fa31b432a9df221419dc086c8c | e1926cf6748fe0fa31b432a9df221419dc086c8c |
| `ERP_APP_ENV` | local | demo | full |
| `ERP_LOG_LEVEL` | DEBUG | INFO | INFO |
| `ERP_DATABASE_URL` | postgresql+asyncpg://erp_app:erp_app@127.0.0.1:5432/smart_erp | postgresql+asyncpg://erp_app:CHANGE_ME@hulkai_aps_postgres:5432/smart_erp | postgresql+asyncpg://erp_app:CHANGE_ME@hulkai_aps_postgres:5432/smart_erp |
| `ERP_MIGRATION_DATABASE_URL` | postgresql+asyncpg://erp_migrator:erp_migrator@127.0.0.1:5432/smart_erp | postgresql+asyncpg://erp_migrator:CHANGE_ME@hulkai_aps_postgres:5432/smart_erp | postgresql+asyncpg://erp_migrator:CHANGE_ME@hulkai_aps_postgres:5432/smart_erp |
| `ERP_DB_NETWORK` | smart-erp-dev_default | hulkai-gz-api_default | hulkai-gz-api_default |
| `ERP_DB_POOL_SIZE` | 2 | 4 | 8 |
| `ERP_DB_MAX_OVERFLOW` | 0 | 4 | 4 |
| `ERP_DB_STATEMENT_TIMEOUT_MS` | 30000 | 30000 | 30000 |
| `ERP_API_BIND` | 127.0.0.1 | 0.0.0.0 | 127.0.0.1 |
| `ERP_API_PORT` | 8010 | 8010 | 8010 |
| `ERP_WEB_PORT` | 8088 | 8088 | 8088 |
| `ERP_API_ROOT_PATH` | /erp/api | /erp/api | /erp/api |
| `ERP_CONNECTOR_GW_URL` | http://connector-gw:8012 | http://connector-gw:8012 | http://connector-gw:8012 |
| `ERP_CONNECTOR_GW_HOST` | 0.0.0.0 | 0.0.0.0 | 0.0.0.0 |
| `ERP_GATEWAY_PORT` | 8012 | 8012 | 8012 |
| `ERP_CONNECTOR_GW_PORT` | 8012 | 8012 | 8012 |
| `ERP_DOC_PORT` | 8020 | 8020 | 8020 |
| `ERP_DOC_SERVICE_URL` | http://doc:8020 | http://doc:8020 | http://doc:8020 |
| `ERP_CONFIG_DIR` | /app/config | /app/config | /app/config |
| `ERP_WORKER_QUEUES` | agent,io | agent,io | agent |
| `ERP_WORKER_CONCURRENCY` | 2 | 4 | 4 |
| `ERP_AGENT_WORKER_CONCURRENCY` | 2 | 4 | 4 |
| `ERP_IO_WORKER_CONCURRENCY` | 1 | 2 | 2 |
| `ERP_HEARTBEAT_INTERVAL_SECONDS` | 30 | 30 | 30 |
| `ERP_SCHEDULER_TICK_SECONDS` | 60 | 60 | 60 |
| `ERP_GW_TIMEOUT_SECONDS` | 30 | 30 | 30 |
| `ERP_GW_EGRESS_ALLOWLIST_FILE` | /app/config/gw-egress.yaml | /app/config/gw-egress.yaml | /app/config/gw-egress.yaml |
| `ERP_OBJECT_STORE_DRIVER` | local | local | minio |
| `ERP_OBJECT_STORE_PATH` | /data/objects | /data/objects | /data/objects |
| `ERP_U8_BASE_URL` | http://127.0.0.1:9090 | http://u8-stub:8021 | http://u8.internal:8080 |
| `ERP_FILE_INBOX` | —（留空=不启用） | /data/inbox | —（留空=不启用） |
| `ERP_METERING_UNIT_PRICE` | 0.05 | 0.20 | 0.20 |
| `ERP_METERING_CURRENCY` | CNY | CNY | CNY |
| `ERP_METERING_UNIT_LABEL` | 行 | 行 | 行 |
| `ERP_METERING_CAP_AMOUNT` | —（用默认值） | 10000 | 100000 |
| `ERP_DB_LOCK_TIMEOUT_MS` | —（用默认值） | —（用默认值） | 3000 |
| `ERP_DB_IDLE_TX_TIMEOUT_MS` | —（用默认值） | —（用默认值） | 60000 |
| `ERP_S3_ENDPOINT` | —（用默认值） | —（用默认值） | http://minio.internal:9000 |
| `ERP_S3_BUCKET` | —（用默认值） | —（用默认值） | smart-erp |
| `ERP_S3_ACCESS_KEY` | —（用默认值） | —（用默认值） | CHANGE_ME |
| `ERP_S3_SECRET_KEY` | —（用默认值） | —（用默认值） | CHANGE_ME |
| `ERP_S3_REGION` | —（用默认值） | —（用默认值） | cn-south-1 |
| `ERP_METERING_PRIOR_AMOUNT` | —（用默认值） | —（用默认值） | 0 |

> `ERP_FILE_INBOX`（文件通道收件箱）：**demo 档默认开启**，connector-gw 以
> `./inbox:/data/inbox:ro` 只读挂载；local / full / prod 留空 = 不启用。
> 显式设置但目录不存在/不可读时 connector-gw 启动失败，不静默降级成「少挂一个连接器」。

> `ERP_DB_MAX_OVERFLOW`：compose 里 **api 固定注入 `${ERP_DB_MAX_OVERFLOW:-0}`**——单 api 进程内有多个
> 独立引擎共用 `erp_app` 的 20 连接上限，overflow 必须为 0；上面 demo/prod 的 `4` 只对
> worker / scheduler / compensation 生效。多副本部署要按 `erp_app` 连接上限重新核算，不要直接放开。

> `ERP_U8_BASE_URL`（U8 写出口）：demo 档由 compose 起 `u8-stub`（`u8-stub` 服务，仅 demo profile），
> api 注入 `http://u8-stub:8021`。**它是模拟外部系统，不是真 U8**：回执/请求留痕只在
> `/stub/receipts`、`/stub/requests`，不会产生任何真实单据；full/prod 必须覆盖为真实地址。

## 3. 差异点与原因

| 差异 | local | demo | prod | 原因 |
|---|---|---|---|---|
| `ERP_IMAGE_TAG` | `dev` | 登记 sha | 登记 sha | 本地允许 `dev`；发布必须 40 位 sha（C12） |
| `ERP_APP_ENV` | `local` | `demo` | `full` | 区分本地调试与两档部署 |
| `ERP_API_BIND` | 回环 | `0.0.0.0` | 回环 | demo 靠安全组限源；生产只经香港 nginx |
| `ERP_OBJECT_STORE_DRIVER` | local | local | minio | 生产要求 Object Lock / WORM（C8） |
| `ERP_WORKER_QUEUES` | agent,io | agent,io | agent | full 档 io 队列由独立 `worker-io` 消费 |
| `ERP_DB_POOL_SIZE` | 2 | 4 | 8 | 并发与连接预算不同；compose 里各服务另有默认值（见 schema `defaultsByService`） |
| `ERP_METERING_*` | 本地小额 | 演示封顶 | 生产封顶 | 计量单价/封顶是配置项，代码不硬编码 |

## 4. 校验

```bash
python scripts/validate_config_schema.py --all
# PASS: 73 个键 · compose 覆盖一致 · env=ok · matrix=demo.env,local.env,prod.env
```

`--all` 做四件事：

1. schema 自身自洽（`required` 与 `properties[*].required` 一致、`type`/`scope` 合法）；
2. compose ↔ schema 双向覆盖：`${ERP_*}` 必须登记、登记为 `compose` 的必须真的被引用；
   `${K:?}` 与 `required` 一致；默认值必须与 schema 一致（按服务差异化调参须写 `defaultsByService`）；
3. 三个 env 文件逐键校验：未知键、整数/布尔类型、枚举、正则、必填非空；
4. 扫描 `apps/` `core/` `tools/` `observability/` `db/` 里按字面量读取的 `ERP_*`，
   每个读点必须同时登记在 schema 与 `.env.example`（代码在读、模板里查不到即红）。

> 可观测性四项（`ERP_TRACE_ENABLED` / `ERP_TRACE_FILE` / `ERP_METRICS_ENABLED` / `ERP_METRICS_PATH`）
> 在三套环境里都不显式设置，取默认值：trace 关闭、metrics 不挂载、路径 `/metrics`。
> 要临时开启时按 `.env.example` 第 9 节改，且开启后必须按 RUNBOOK 5.2 在反向代理上限定内网。

## 5. 与发布清单的关系

发布时以 `env/<环境>.env` 为取值依据，但真正生效的是部署机上那份 `.env`（由密管渲染）。
本目录的文件是**取值证据**：键、形态与默认值必须与 `.env.example`、schema、compose 三方一致；
任何一方改动而未同步，`validate_config_schema.py --all` 会红。
