# 云上线执行单：并入 hulkai / 广州云服务器

> 用途：把 smart-ERP 的四个镜像并入现有 hulkai 工程，部署到广州云服务器。
> 本单按顺序执行，每一步都有「命令 / 预期 / 失败动作」。
> 标注 **[hk-web]** 的步骤依赖 hk-web 变更单；变更单未落地前，只能做回环验收，不能做公网验收。
>
> 演练信息：2026-11-11 在本机用 `fa5de4f786109ec1280b09afe33190892e03f102` 实跑，
> 输出见附录 A。凡本机可执行的命令均已实际执行；
> **未执行**：ssh/scp 传输、在 hulkai PG 容器内执行 provision、公网域名健康检查
> （本机无目标主机/无 hk-web 权限，依赖 hk-web 变更单）。

## 0. 变量与硬前提

```bash
export NEW_SHA=fa5de4f786109ec1280b09afe33190892e03f102   # 待发布 main，40 位
export OLD_SHA=5ef1df9a263b45c028134d02e44f25d2c3125da3   # 当前线上，回滚用
export DEPLOY_DIR=/opt/smart-erp
export GZ_HOST=<广州云服务器 IP>
export DB_NETWORK=hulkai-gz-api_default
export DB_HOST=hulkai_aps_postgres
export DB_NAME=smart_erp
export DEMO_TOKEN=demo-session-token-2026-11-11
```

硬前提：

- 服务器已并入 hulkai 网络 `hulkai-gz-api_default`，且能解析 `hulkai_aps_postgres`。
- 公网入口只有 hk-web；`erp_api` 只绑 `127.0.0.1:8010`，`erp_web` 只绑 `127.0.0.1:8088`（compose 已如此配置）。
- 目标库名必须是 `smart_erp`；脚本对其它库名直接拒绝。
- `pgvector` 不是硬依赖：缺了只告警，不阻断迁移与启动。

## 1. 冻结版本（主仓执行）

```bash
git fetch origin
test "$(git rev-parse origin/main)" = "$NEW_SHA" && echo "SHA_OK"
```

预期：输出 `SHA_OK`。
失败动作：sha 不一致就停手，重新确认要发的 main；不要用分支头或 PR 合并前 sha 代替。

## 2. 构建四个镜像并校验（主仓执行）

```bash
ERP_BUILD_WEB_BASE=/ ./scripts/build-images.sh --tag "$NEW_SHA"
cd dist && sha256sum -c sha256sum.txt
```

预期：

- 四个镜像 `smart-erp/{app,connector-gw,web,doc}:$NEW_SHA` 全部构建成功；
- 输出 `<tar 文件名>: OK`。

2026-11-11 实测（样例）：

```
tag        fa5de4f786109ec1280b09afe33190892e03f102
tar        dist/erp-images-fa5de4f786109ec1280b09afe33190892e03f102.tar.gz
tar sha256 1d35454d6b438d49358fceeb9a0833a56aa7c90488afa395890cf792f8e665bb
```

失败动作：sha 校验失败直接重传/重建，不允许“先上线再说”。

## 3. 传到广州并加载镜像

推荐直接用仓库脚本（会做本地校验、远端校验、`docker load`、按 demo 档起栈）：

```bash
./scripts/deploy-images.sh --tag "$NEW_SHA"
```

已在本地用 `--dry-run` 验证它实际会执行：

```bash
ssh root@"$GZ_HOST" "mkdir -p '$DEPLOY_DIR/dist' && install -d -m 0750 -o 10001 -g 10001 '$DEPLOY_DIR/inbox'"
scp dist/erp-images-"$NEW_SHA".tar.gz dist/sha256sum.txt root@"$GZ_HOST":"$DEPLOY_DIR/dist/"
scp deploy/smart-erp/docker-compose.yml deploy/smart-erp/.env.example root@"$GZ_HOST":"$DEPLOY_DIR/"
ssh root@"$GZ_HOST" "cd '$DEPLOY_DIR/dist' && sha256sum -c sha256sum.txt && docker load -i 'erp-images-$NEW_SHA.tar.gz'"
ssh root@"$GZ_HOST" "test -f '$DEPLOY_DIR/.env' || { echo 'ERROR: $DEPLOY_DIR/.env 不存在'; exit 3; }"
```

失败动作：

- 远端 `sha256sum -c` 失败 → 重新 scp，不要跳过；
- `/opt/smart-erp/.env` 不存在 → 先按 `.env.example` 填好再继续（脚本 exit 3 就是拦这个）。

如果目标不是 demo 档，第 4 步起把 `--profile demo` 换成 `--profile full`，只起 `api connector-gw web`：

```bash
cd "$DEPLOY_DIR" && ERP_IMAGE_TAG="$NEW_SHA" docker compose --profile full up -d api connector-gw web
```

已验证 `--profile full` 的 compose 配置可解析（`config --quiet` 通过）。

## 4. PostgreSQL 版本与扩展检查

```bash
docker run --rm --network "$DB_NETWORK" -e PGPASSWORD="<erp_migrator 口令>" postgres:15 \
  psql -h "$DB_HOST" -U erp_migrator -d "$DB_NAME" \
  -c "SHOW server_version" \
  -c "SELECT extname, extversion FROM pg_extension ORDER BY extname"
```

预期（2026-11-11 实测，PG 15.19）：

```
pg_trgm   1.6
pgcrypto  1.3
plpgsql   1.0
```

`vector` 可选：没有 `vector` 行时只记录告警，继续执行；不得因为缺 pgvector 中断发版。

## 5. 迁移前必须备份

```bash
ERP_IMAGE_TAG="$NEW_SHA" \
ERP_BACKUP_DATABASE_URL="postgresql+asyncpg://erp_migrator:<口令>@$DB_HOST:5432/$DB_NAME" \
ERP_DB_NETWORK="$DB_NETWORK" \
./scripts/backup-smart-erp.sh --out "$DEPLOY_DIR/backups"
```

预期：输出当天目录、`.dump` 文件名、sha256，并打印表数与 alembic 版本。
失败动作：非 0 退出 → 取消本次发版（迁移不可自动回退，没备份不许动）。

## 6. 初始化 / 迁移（顺序不可换）

顺序固定：`00-provision.sql` → `alembic upgrade head` → demo 导入。

### 6.1 开通库/角色/扩展（首次必做，重跑安全）

```bash
docker exec -i "$PG_CONTAINER" psql -q -U postgres -d postgres \
  -v ON_ERROR_STOP=1 \
  -v erp_app_pwd="<erp_app 口令>" -v erp_mig_pwd="<erp_migrator 口令>" \
  -f - < "$DEPLOY_DIR/db/00-provision.sql"
```

`$PG_CONTAINER` 是 hulkai 的 PG 容器名。`vector` 缺失时该脚本只告警。
已在本地一键起栈中实际执行通过（provision 后迁移成功）。

### 6.2 迁移到 head

```bash
docker run --rm --network "$DB_NETWORK" \
  -e ERP_APP_ENV=demo \
  -e "ERP_DATABASE_URL=postgresql+asyncpg://erp_migrator:<口令>@$DB_HOST:5432/$DB_NAME" \
  --entrypoint alembic "smart-erp/app:$NEW_SHA" -c /app/alembic.ini upgrade head
```

预期：迁移链按 `0001 → … → 0011_job_lease_gen` 顺序执行到 head。

```bash
docker run --rm --network "$DB_NETWORK" \
  -e "ERP_DATABASE_URL=postgresql+asyncpg://erp_migrator:<口令>@$DB_HOST:5432/$DB_NAME" \
  --entrypoint alembic "smart-erp/app:$NEW_SHA" -c /app/alembic.ini current
```

预期：`0011_job_lease_gen (head)`，与备份 `.meta.txt` 记录一致。

失败动作：迁移报错立即停手，先用第 10 节的恢复流程回到备份，不要在生产库上手工改表。

## 7. demo 数据导入（仅 demo 环境）

```bash
docker run --rm --network "$DB_NETWORK" \
  -v "$DEPLOY_DIR/demo:/repo/demo:ro" -w /repo \
  --entrypoint python "smart-erp/app:$NEW_SHA" /repo/demo/load.py \
  --scenario manufacturing-multi-plant \
  --dsn "postgresql://erp_migrator:<口令>@$DB_HOST:5432/$DB_NAME"
```

前提：`demo/` 目录已随镜像包之外的部署物传到 `$DEPLOY_DIR/demo`（`deploy-images.sh` 不传该目录，需要单独 scp/rsync 一次）。

导入后核对（2026-11-11 实测值）：

```bash
docker exec -e PGPASSWORD="<口令>" "$PG_CONTAINER" \
  psql -U erp_migrator -d "$DB_NAME" -c "
    SELECT 'ticket' t, count(*) FROM erp.ticket
    UNION ALL SELECT 'ticket_step', count(*) FROM erp.ticket_step
    UNION ALL SELECT 'approval_record', count(*) FROM erp.approval_record
    UNION ALL SELECT 'recon_batch', count(*) FROM erp.recon_batch
    UNION ALL SELECT 'audit_image', count(*) FROM erp.audit_image"
```

预期：`16 / 20 / 25 / 3 / 18`。不一致先查导入日志，不要用页面空态倒推。

## 8. 启动业务容器

```bash
cd "$DEPLOY_DIR"
ERP_IMAGE_TAG="$NEW_SHA" docker compose --profile full up -d api connector-gw web
```

预期：三容器 healthy；`docker ps` 里镜像 tag 全是 `$NEW_SHA`。

## 9. 四类健康检查

### 9.1 回环（不依赖 hk-web）

```bash
curl -fsS -o /dev/null -w 'api /healthz          %{http_code}\n' http://127.0.0.1:8010/healthz
curl -fsS -o /dev/null -w 'web /erp/api/healthz  %{http_code}\n' http://127.0.0.1:8088/erp/api/healthz
curl -fsS -o /dev/null -w 'web /                 %{http_code}\n' http://127.0.0.1:8088/
curl -fsS -H "X-Session-Token: $DEMO_TOKEN" http://127.0.0.1:8010/v1/auth/session
```

预期（2026-11-11 实测全部 200）：

```
api /healthz          200
web /erp/api/healthz  200
web /                 200
/v1/auth/session      200 + {"user_id":...,"default_scope":{"org_id":"ORG-SZ","period":"2026-09","system_ids":["sap","u8","wms"]}}
```

`/v1/auth/session` 带令牌返回 200 的前提：服务端设置

```
ERP_APP_ENV=demo
ERP_DEMO_SESSION_ENABLED=true
ERP_DEMO_SESSION_IDENTITIES=[{"token":"demo-initiator-token-2026-11-11","user_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","tenant_id":"a927f664-796d-5a6c-bb43-c052cd4a0e2e","roles":["operator"],"org_ids":["ORG-SZ"],"periods":["2026-07","2026-08","2026-09"],"system_ids":["u8","wms","sap"]},{"token":"demo-reviewer-token-2026-11-11","user_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","tenant_id":"a927f664-796d-5a6c-bb43-c052cd4a0e2e","roles":["reviewer","finance"],"org_ids":["ORG-SZ"],"periods":["2026-07","2026-08","2026-09"],"system_ids":["u8","wms","sap"]},{"token":"demo-cfo-token-2026-11-11","user_id":"dddddddd-dddd-4ddd-8ddd-dddddddddddd","tenant_id":"a927f664-796d-5a6c-bb43-c052cd4a0e2e","roles":["reviewer","approver","cfo"],"org_ids":["ORG-SZ"],"periods":["2026-07","2026-08","2026-09"],"system_ids":["u8","wms","sap"]},{"token":"demo-approver-2-token-2026-11-11","user_id":"eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee","tenant_id":"a927f664-796d-5a6c-bb43-c052cd4a0e2e","roles":["reviewer","approver","finance"],"org_ids":["ORG-SZ"],"periods":["2026-07","2026-08","2026-09"],"system_ids":["u8","wms","sap"]}]
```

IDENTITIES 是 JSON 数组，每条身份各自声明 roles / orgs / periods / systems。
演示 W2 双人复核：`bbbb...` 的 reviewer 票由种子预置；现场先用 `dddd...`（CFO，
无 finance）点 approver 被类别闸拒，再用 `eeee...`（持有 finance）点 approver 通过
（`satisfied=true`），最后由 `aaaa...` 触发执行。`dddd...` 故意不给 finance，
它是「规则真的在拦」的反例。
身份不全 / 坏 UUID / 令牌或 user_id 重复 / 空映射都会让 api 启动失败。

正式生产必须 `ERP_DEMO_SESSION_ENABLED=false`，改用真实令牌验收。

### 9.2 公网（**依赖 hk-web 变更单**）

```bash
curl -fsS -o /dev/null -w 'public api %{http_code}\n' https://<公网域名>/erp/api/healthz
curl -fsS -o /dev/null -w 'public web %{http_code}\n' https://<公网域名>/demo/smart-erp/
curl -fsS -H "X-Session-Token: $DEMO_TOKEN" https://<公网域名>/erp/api/v1/auth/session
```

预期：均 200。变更单未落地时，这三条会 404/502，属预期，不能据此判定应用故障。

## 10. 回滚

### 10.1 镜像回滚（分钟级，已实测）

```bash
cd "$DEPLOY_DIR"
ERP_IMAGE_TAG="$OLD_SHA" docker compose --profile full up -d api connector-gw web
curl -fsS -o /dev/null -w 'rollback /healthz %{http_code}\n' http://127.0.0.1:8010/healthz
```

2026-11-11 实测：切到 `5ef1df9…` 后 `/healthz` 200、`/v1/auth/session` 200；再切回 `fa5de4f…` 仍 200。

### 10.2 数据库恢复（危险，必须先备份）

```bash
ERP_DB_NETWORK="$DB_NETWORK" \
./scripts/restore-smart-erp.sh \
  --dump "$DEPLOY_DIR/backups/<日期>/smart_erp-<时间戳>-<sha12>.dump" \
  --target-url "postgresql+asyncpg://erp_migrator:<口令>@$DB_HOST:5432/$DB_NAME" \
  --force
```

2026-11-11 实测（恢复到独立库 `smart_erp_restore_check2`）：

```
一致性：备份包对象=44；恢复库对象=44
一致性校验通过：库内对象集合 − 备份包对象集合 = 空
核对：erp schema 表数=23；alembic 版本=0011_job_lease_gen
行数：ticket=16 ticket_step=20 approval_record=25 recon_batch=3 audit_image=18
```

注意：

- 本次同时修了恢复脚本的一致性校验：原先备份包只统计 `erp/public`，恢复库却统计所有 schema（含 `demo`），只要库里有 demo 数据就必然误报 exit 5；现在两边都只看 `erp/public`。
- 恢复脚本不会自动跑迁移；恢复后按第 6.2 节执行 `alembic upgrade head`（只前进，不回退）。
- 迁移不可自动回退；若备份版本高于代码预期，先评估 `alembic downgrade` 影响面，不要在线上直接降级。

## 11. hk-web 变更单依赖清单

| 步骤 | 是否依赖 hk-web | 说明 |
| --- | --- | --- |
| 1–8 构建/传输/迁移/起栈 | 否 | 只在本机与服务器回环内 |
| 9.1 回环四类检查 | 否 | 127.0.0.1:8010 / 8088 |
| 9.2 公网 `/erp/api/healthz` | **是** | hk-web 需 `location /erp/api/ → 127.0.0.1:8010` |
| 9.2 公网首页 `/demo/smart-erp/` | **是** | hk-web 需 rewrite 到 `127.0.0.1:8088/` |
| 9.2 公网 `/erp/api/v1/auth/session` | **是** | 同上，同一段 proxy |
| 10.1 镜像回滚 | 否 | 回环即可验证 |

hk-web 变更单最小内容（与现有香港边缘一致）：

```nginx
location /erp/api/ {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
location /demo/smart-erp/ {
    proxy_pass http://127.0.0.1:8088/;
}
```

变更单未落地前的验收口径：第 9.1 节四条全 200，即视为应用侧就绪；公网 404 不算应用失败。

## 附录 A：2026-11-11 本地演练记录

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 版本冻结 | `git rev-parse origin/main` | `fa5de4f786109ec1280b09afe33190892e03f102` |
| 四镜像构建 | `ERP_BUILD_WEB_BASE=/ scripts/build-images.sh --tag …` | 四个 tag 构建成功 |
| tar 校验 | `sha256sum -c sha256sum.txt` | `OK` |
| 传输脚本 | `deploy-images.sh --tag … --dry-run` | 5 步命令全部展开成功 |
| compose 配置 | `docker compose --profile full config --quiet` | `CONFIG_OK` |
| PG 版本/扩展 | `SHOW server_version` + `pg_extension` | `15.19`；`pg_trgm 1.6`、`pgcrypto 1.3`、`plpgsql 1.0`；无 `vector`（走告警路径） |
| 四类健康检查 | 第 9.1 节四条 | `200 / 200 / 200 / 200`；session 返回 `default_scope` |
| 镜像回滚 | 第 10.1 节 | `5ef1df9` 时 200，切回 `fa5de4f` 仍 200 |
| 备份 | `backup-smart-erp.sh --out … --skip-objects` | 产出 `.dump` + sha256 |
| 恢复 | `restore-smart-erp.sh --force` | 44=44、alembic `0011`、行数 16/20/25/3/18 |

未在本机执行（原因：无广州主机 / 无 hk-web 权限）：第 3 节 ssh/scp、第 6.1 节 hulkai PG 容器内执行、第 9.2 节公网域名访问。这些命令均取自仓库现有脚本或 `--dry-run` 实际展开结果，未使用推测写法。
