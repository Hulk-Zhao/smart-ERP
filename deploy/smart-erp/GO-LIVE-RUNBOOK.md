# 上线日操作单（v0.2.0 · demo 档）

> **用途**：上线当天从「变更窗口前 30 分钟」到「上线后 2 小时观察期结束」，按时间顺序照着敲。
> 背景与细则见 [RUNBOOK.md](./RUNBOOK.md)；发布准入见 [RELEASES.md](./RELEASES.md)；服务就绪矩阵见 [FULL-READINESS.md](./FULL-READINESS.md)。
> 所有命令在**仓库根目录**或脚本所在目录执行，已在命令里标出 `# cwd:`。

## 0. 标记与时间轴

| 标记 | 含义 |
|---|---|
| ✅ | 现在就能执行，已在本机/演练环境验证过命令形态 |
| 🔴 R*n* | **当前不可执行**，原因见 §7（不是"跳过"，是"做了也不成立"） |
| ⚠️ | 可执行，但结果依赖人工判断 |

| 时刻 | 段落 | 关键动作 | 预计耗时 | 红项 |
|---|---|---|---|---|
| T-30 | §1 冻结与备份 | 锁版本、拉包校验、发布前备份 | 10–20 min | R1 |
| T-10 | §2 窗口前最后检查 | 安全组、hk-web nginx、端口 | 5–10 min | R2 R3 |
| T0 | §3 发布 | 传镜像、迁移、起栈、三层探针 | 10–25 min | R4 |
| T+10 | §4 六项核对 | 6 个数字（含迁移版本） | 5–10 min | R5 R6 R7 |
| T+30 | §5 观察期 | 每 15 min 巡检，至 T+2h | 90 min | R8 |
| 任意 | §6 回滚 | 镜像回退 / 备份恢复 | 10–60 min | — |

**本单只覆盖 demo 档（`api` + `connector-gw` + `web`）。** full 档（`api-prod` / `migrate` / `worker-agent` / `worker-io` / `scheduler` / `doc`）在 B-S8-01 解除前禁止发布，相关步骤统一标 🔴 R9。

---

## 1. T-30 → T-10：冻结版本与发布前备份

### G01 冻结版本 sha ✅（约 1 min）

```bash
# cwd: 仓库根
git ls-remote origin refs/heads/main
```

- **预期**：一行 `<40 位 sha>` + `refs/heads/main`。把这个 sha 记为 `NEW_SHA`，写进台账与 `RELEASES.md`。
- **失败判定/动作**：无输出或网络失败 → **停止发版**；不允许用本地 `git rev-parse HEAD` 或未提交工作树代替（`RELEASE-CHECKLIST.md` 第 0 条硬门禁）。
- **耗时**：< 1 min。

### G02 构建产物校验 ✅（约 1–2 min）

```bash
# cwd: 构建机 dist 目录
sha256sum -c sha256sum.txt
ls -lh erp-images-${NEW_SHA}.tar.gz
```

- **预期**：`erp-images-<sha>.tar.gz: OK`；tar 包大小与实际镜像总和大体相符。
- **失败判定/动作**：`FAILED` → 重新 `ERP_IMAGE_TAG=${NEW_SHA} scripts/build-images.sh` 构建；**不要**跳过校验直接传。
- **耗时**：1–2 min。

### G03 发布前备份 ✅（约 3–15 min，视数据量）

```bash
# cwd: 仓库根（服务器上）
ERP_IMAGE_TAG=${NEW_SHA} \
ERP_BACKUP_DATABASE_URL='postgresql+asyncpg://erp_migrator:***@hulkai_aps_postgres:5432/smart_erp' \
scripts/backup-smart-erp.sh --out /opt/smart-erp/backups
```

- **预期**：当天目录下出现 `smart_erp-<时间戳>-<sha12>.dump`、`erp_objects-<时间戳>-<sha12>.tar.gz`、`.meta.txt`，`SHA256SUMS` 追加三行；脚本打印表数与 `alembic` 版本。
- **失败判定/动作**：exit 3 = 库名不是 `smart_erp`（红线，停手查连接串）；其它非 0 → 重跑一次，仍失败则**取消本次发版**（迁移不可自动回退，没备份不许动）。
- **耗时**：3–15 min（演练数据 40KB 秒级，生产按实际表量估）。

### G04 备份产物自检 ✅（约 1 min）

```bash
# cwd: /opt/smart-erp/backups/<yyyy-mm-dd>
sha256sum -c SHA256SUMS
pg_restore -l smart_erp-*.dump | head -30
```

- **预期**：三个产物全部 `OK`；`pg_restore -l` 能列出 TOC（只读目录，不连库）。
- **失败判定/动作**：任一 `FAILED` → 回到 G03 重做，本单后续全部暂停。
- **耗时**：< 1 min。

### G05 备份异地/离线拷贝 🔴 R1（需客户侧配合，约 5–20 min）

> **当前不可执行**：需要客户侧提供备份机或对象存储，且开通从业务机到备份机的通道。没有异地副本则按 RUNBOOK §1.2「视为不达标」，但**不阻塞**本次 demo 上线，只登记为遗留。

---

## 2. T-10 → T0：窗口前最后检查

### G06 云安全组核对 🔴 R2（云控制台人工，约 3–5 min）

> **当前不可执行（自动）**：只能在云控制台人工核对。判定口径：
> - 8010 **不得**出现 `0.0.0.0/0` / `::/0`；只允许香港反代固定出口 IP；
> - 8088 同理；
> - 8011 / 8012 / 8020 一律不得在安全组放行。
>
> **gating**：若目标机有公网 IP 而 demo 档 `api` 绑定 `0.0.0.0:8010`（RUNBOOK §5.1），**R2 未收口就不得上线**——这是本单唯一"红项阻塞上线"的一条。核对完成后在台账记一条"安全组已核对 + 截图位置"。

### G07 hk-web nginx 片段落地 🔴 R3（hk-web 人工，约 5–10 min）

> **当前不可执行（不在本仓库）**：需要 hk-web 维护人把 RUNBOOK §5.2 的片段写进 nginx 并 `nginx -t && nginx -s reload`。要点两条：
> - `location = /metrics { deny all; }`（或 `return 404;`）；
> - `location /erp/api/ { proxy_pass http://127.0.0.1:8010; }`，**结尾不带 `/`**，保留 `/erp/api/` 前缀；
> - `/demo/smart-erp/*` 去前缀后打到 `127.0.0.1:8088`。
> 未落地则 G14/G15 的公网验收无法成立。

### G08 宿主端口占用检查 ✅（约 1 min）

```bash
# cwd: 服务器任意目录
ss -lntp | grep -E ':(8010|8088)\b' || echo "ports free"
```

- **预期**：首发机输出 `ports free`；升级时可能看到旧容器占用。
- **失败判定/动作**：被非本栈进程占用 → 停止，先查清占用方；被旧栈占用 → 按 G09 处理。
- **耗时**：< 1 min。

### G09 旧栈下线（仅升级/换档时）✅（约 1–2 min）

```bash
# cwd: /opt/smart-erp
docker compose -f docker-compose.yml --env-file .env ps
# 有旧栈时：
docker compose -f docker-compose.yml --env-file .env --profile demo down
```

- **预期**：`docker compose ps` 为空。
- **失败判定/动作**：`Error: ... container name erp_api is already in use` → 说明跨档 `up` 撞名（RUNBOOK §5.1），必须先 `down` 掉旧档。
- **耗时**：1–2 min。**数据边界**：`down` 不删卷，`erp_objects` 数据保留。

### G10 配置自检 ✅（约 1 min）

```bash
# cwd: /opt/smart-erp
docker compose -f docker-compose.yml --env-file .env --profile demo config --quiet && echo CONFIG_OK
```

- **预期**：`CONFIG_OK`（缺必填变量时 compose 会直接报变量名并失败）。
- **失败判定/动作**：报缺变量 → 补 `.env`；**不要**为了跑通把必填项留空。
- **耗时**：< 1 min。

---

## 3. T0 → T+10：发布执行（demo 档）

### G11 传镜像并加载 ✅（约 5–20 min，视带宽与包大小）

```bash
# cwd: 构建机（仓库根）
ERP_DEPLOY_HOST=<服务器 IP> ERP_DEPLOY_USER=root ERP_DEPLOY_DIR=/opt/smart-erp \
scripts/deploy-images.sh --tag ${NEW_SHA}
# 可选：只传不重启（先看链路）
scripts/deploy-images.sh --tag ${NEW_SHA} --no-up
```

- **预期**：远端 `sha256sum -c` 通过；`docker load` 后 `docker image ls smart-erp/*` 出现四个同 sha 镜像。
- **失败判定/动作**：sha 校验失败 → 重传，连续两次失败停止发版；`docker load` 报错 → 检查 tar 是否被截断（回到 G02）。
- **耗时**：5–20 min。

### G11a 首次初始化顺序：先 `db/00-provision.sql`，再迁移

> **仅首次部署 / 新库需要；顺序不可颠倒。** `db/00-provision.sql` 是角色与默认权限的唯一来源，
> `db/migrations/` 对 `ticket` / `job_queue` / `compensation_task` 等业务表没有任何 `GRANT`。
> 跳过这一步：库能迁、服务能起、`/healthz` 可能 200，直到第一次写入才报
> `permission denied for table ...`。执行方法与自检 SQL 见 [RUNBOOK §0](./RUNBOOK.md)。

- **顺序**：G11a（`00-provision.sql`）→ **G11b（`alembic upgrade head`）** → G12（起栈）。
- **自检**：迁移完成后按 RUNBOOK §0 的 SQL 查 `information_schema.role_table_grants`，
  应用直接读写的业务表应各自含 `DELETE,INSERT,SELECT,UPDATE`；出现 `(none)` 即开通顺序漏了。
  以当前 migration head 为准：表清单会随迁移变化，这条只验权限，不锁表名与数量。
- **角色不要混**：生产 / 网关 `erp_app`，迁移 `erp_migrator`，CI `erp_test`。

### G11b 执行数据库迁移（仅当镜像 head ≠ 库内版本）✅（约 1–5 min）

> **顺序在起 api 之前**：镜像换了、库没迁，是最典型的"看起来起来了、其实是半截"。

```bash
# cwd: /opt/smart-erp
# ① 读镜像预期 head（只读脚本，不连库）
docker run --rm smart-erp/app:${NEW_SHA} alembic -c /app/alembic.ini heads

# ② 读库内当前版本
set -a; . .env; set +a
docker run --rm --network "${ERP_DB_NETWORK:-hulkai-gz-api_default}" postgres:15-alpine \
  psql "$(printf '%s' "$ERP_DATABASE_URL" | sed -E 's#^postgresql\+asyncpg:#postgresql:#')" -Atc \
  "select coalesce(max(version_num),'NONE') from erp.alembic_version_erp;"

# ③ 仅当 ② ≠ ① 时执行；等价于 compose 的 migrate 服务命令，角色必须是 erp_migrator
docker run --rm --network "${ERP_DB_NETWORK:-hulkai-gz-api_default}" \
  -e ERP_DATABASE_URL="${ERP_MIGRATION_DATABASE_URL}" \
  smart-erp/app:${NEW_SHA} alembic -c /app/alembic.ini upgrade head
```

- **预期**：① 只打印一行 `<revision> (head)`（多行 = 迁移脚本分叉，停止发布）；② 与 ① 相同则**跳过 ③**；③ 逐条打印 `Running upgrade ... -> ...`，最后重跑 ② 与 ① 一致。
- **失败判定/动作**：③ 中断 → **不要起 api**，走 §6.3（迁移不可自动回退，只有备份能救）；权限拒绝 → 确认用的是 `ERP_MIGRATION_DATABASE_URL`（`erp_migrator`），不是业务角色 `erp_app`。
- **耗时**：1–5 min。
- **与 R9 的边界**：这里只是用 `docker run` 跑镜像内的一次性 alembic，**不 `up` full profile 的 `migrate` 服务**，也不起 `api-prod` / `worker-*` / `doc`。

### G12 起栈（只起三个服务）✅（约 1–2 min）

```bash
# cwd: /opt/smart-erp
ERP_IMAGE_TAG=${NEW_SHA} docker compose -f docker-compose.yml --env-file .env \
  --profile demo up -d api connector-gw web
```

> 只点名三个服务：本版范围就是 `api` + `connector-gw` + `web`（`RELEASES.md` 准入条款第 1 条）。
> demo 档的 `worker` / `scheduler` 同属 demo profile，但**不要**起（R5–R8）。

- **预期**：三个容器 `Started`；`docker compose ps` 中 `api` / `erp_gw` 在 60s 内 healthy，`erp_web` 在 10s 内 healthy。
- **失败判定/动作**：容器反复重启 → `docker logs --tail 100 erp_api` 查配置/连接串；`host not found in upstream` 不会出现在 web（已改为变量式反代）。起不来且 15 min 内无法定位 → 走 §6.2 镜像回退到旧 sha。
- **耗时**：1–2 min。

### G13 三层探针 ✅（约 3 min）

```bash
# cwd: 服务器任意目录
docker compose -f /opt/smart-erp/docker-compose.yml --env-file /opt/smart-erp/.env --profile demo ps
docker port erp_api
curl -s -o /dev/null -w 'api=%{http_code}\n'      http://127.0.0.1:8010/healthz
curl -s -o /dev/null -w 'web=%{http_code}\n'      http://127.0.0.1:8088/healthz
curl -s -o /dev/null -w 'web_index=%{http_code}\n' http://127.0.0.1:8088/
```

- **预期**：三个容器 `healthy`；`docker port erp_api` = `8010/tcp -> 127.0.0.1:8010`（**出现 `0.0.0.0:8010` 即不合格**，见 G06）；`api=200`、`web=200`、`web_index=200`。
- **失败判定/动作**：api 非 200 → 查 `docker logs erp_api`；web 非 200 → 查 `docker logs erp_web`；端口绑定错 → 说明用的是 demo 档 `api`（0.0.0.0），立即停栈并走 R2 收口，不得带病上线。
- **耗时**：约 3 min（含 60s `start_period` 等待）。

### G14 公网链路验收 🔴 R4（依赖 R2 + R3，约 3 min）

> **当前不可执行**：R2（安全组）/ R3（hk-web nginx）未完成时，公网链路一定不通，跑了也是假红。
> 前置满足后执行：
> ```bash
> curl -s -o /dev/null -w 'public_api=%{http_code}\n' https://<域名>/erp/api/healthz
> curl -s -o /dev/null -w 'public_web=%{http_code}\n' https://<域名>/demo/smart-erp/
> curl -m 3 -sS -o /dev/null -w 'metrics=%{http_code}\n' https://<域名>/metrics
> curl -m 3 -sS -o /dev/null -w 'metrics_direct=%{http_code}\n' http://<公网IP>:8010/metrics
> ```
> 预期 `public_api=200`、`public_web=200`；`metrics_direct` 必须连接失败；`metrics` 必须 **403**
> （404 不算通过——与「端点未挂载」同码，完整判定见 §4.4 三行矩阵）。

### G15 页面实测 ⚠️（约 3 min）

- 浏览器打开 `https://<域名>/demo/smart-erp/`，确认：无白屏、DevTools Network 里 `assets/index-*.js|css` 全 200（**不能出现站点根 `/assets/` 的 404**）。
- **失败判定/动作**：白屏且资源 404 → `ERP_WEB_BASE` 失效类问题（`tests/deploy/test_web_artifact_prefix.py` 专防此项）；立即 `docker run --rm --entrypoint cat smart-erp/web:${NEW_SHA} /usr/share/nginx/html/index.html | grep -o '/[^"]*assets[^"]*'` 核对前缀，不符 → 回退到上一 sha。
- **耗时**：约 3 min。

---

## 4. T+10 → T+15：上线后必须核对的 6 个数字

> 第 1–3 条与第 6 条在 T+10 与 T+2h 各做一次；第 4、5 条一次即可。**6 个数字全过才算上线完成。**

### 4.1 `/healthz` 200 ✅

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/healthz
```

- 通过：`200`（连续 3 次，间隔 10s）。
- 不通过：见 G13 失败动作；15 min 未恢复 → §6.2。

### 4.2 `job_queue` 待处理数 🔴 R5（worker 未交付）

> **当前不可执行**：worker 常驻进程未交付，队列没有消费者，这个数字只会涨。现在读它没有判断意义。
> 读取命令（S3 交付后启用）：
> ```bash
> # cwd: 仓库根
> set -a; . deploy/smart-erp/.env; set +a
> docker run --rm --network "${ERP_DB_NETWORK:-hulkai-gz-api_default}" postgres:15-alpine \
>   psql "$(printf '%s' "$ERP_DATABASE_URL" | sed -E 's#^postgresql\+asyncpg:#postgresql:#')" -Atc \
>   "select coalesce(sum(n),0) from (select count(*) n from erp.job_queue where state='queued' group by queue) s;"
> ```
> 通过口径（交付后）：多次采样**不单调增长**，且有 `state='running'` → `done` 的迁移记录。

### 4.3 `worker_heartbeat` 最新时间 🔴 R6（worker 未交付）

> **当前不可执行**：没有 worker 就不会有行，查出来恒为空。
> 读取命令（S3 交付后启用）：
> ```bash
> # 同 4.2 的 psql 前缀
> ... -Atc "select coalesce(to_char(max(heartbeat_at) at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),'EMPTY'), coalesce((extract(epoch from now()-max(heartbeat_at))::int),-1) from erp.worker_heartbeat;"
> ```
> 通过口径（交付后）：非 `EMPTY`，且 age < 300s（`tools.health` 的默认 stale 阈值）。

### 4.4 `/metrics` 从公网不可达 🔴 R4（依赖 R2/R3）

> **当前不可执行**：与 G14 同源，安全组与 hk-web 未落地前无法判定。
> 与 RUNBOOK §5.2 同一口径，按 1 → 2 → 3 顺序执行。
>
> 判断原则：**只有第 2 行（开启后容器内直连 200）能证明端点存在，第 3 行才有意义。**
> 跳过第 2 行直接看第 3 行，等于在验一个可能不存在的端点——`#48` 的 deny 规则就是
> 这样保护了一个不存在的端点，靠 404 通过，属于假通过。
>
> | # | 操作 | 期望 | 证明了什么 |
> |---|---|---|---|
> | 1 | `.env` 里 `ERP_METRICS_ENABLED=`（空）或 `0`，重建 api 后容器内探针 | **404** | 关闭时端点不暴露 |
> | 2 | `.env` 里 `ERP_METRICS_ENABLED=1`，重建 api 后容器内探针 | **200**，且正文含 `smart_erp_recon_batches_total` | 端点确实挂载、指标在产出 |
> | 3 | 保持第 2 行开启状态，`curl -m 3 -o /dev/null -sS -w '%{http_code}\n' https://<域名>/erp/api/metrics` | **403** | nginx 的 deny 生效，不是「端点不存在」 |
>
> 第 3 行返回 **404 不算通过**：与第 1 行同码，既可能是 nginx 写成 `return 404`，也可能是没 reload；
> 返回 200 即指标外泄，立即回滚并收口。站点根路径兜底 `https://<域名>/metrics` 也必须是 **403**。
> `http://<公网IP>:8010/metrics` 必须连接失败（curl exit 7/28）。容器内探针与完整命令见 RUNBOOK §5.2。

### 4.5 备份产物 `SHA256SUMS` 校验通过 ✅

```bash
# cwd: /opt/smart-erp/backups/<yyyy-mm-dd>
sha256sum -c SHA256SUMS
```

- 通过：三行全部 `OK`（与 G04 同一份文件，此处是上线后的复核）。

### 4.6 `alembic_version_erp` 与发布 sha 的预期迁移号一致 ✅

```bash
# cwd: /opt/smart-erp
# ① 镜像预期 head（只读脚本，不连库）
docker run --rm smart-erp/app:${NEW_SHA} alembic -c /app/alembic.ini heads

# ② 库内实际版本
set -a; . .env; set +a
docker run --rm --network "${ERP_DB_NETWORK:-hulkai-gz-api_default}" postgres:15-alpine \
  psql "$(printf '%s' "$ERP_DATABASE_URL" | sed -E 's#^postgresql\+asyncpg:#postgresql:#')" -Atc \
  "select coalesce(max(version_num),'NONE') from erp.alembic_version_erp;"
```

- 通过：② 的输出等于 ① 的 head，且与 `RELEASES.md` 中该 sha 登记的值一致；`heads` 只有一行。
- 不通过（库内**落后**）：镜像换了、迁移没跑 → 按 **G11b** 补迁移再复测；不得带着旧版本进观察期。
- 不通过（库内**超前/未知**）：镜像不是这一版的迁移脚本，或有人手工跑过迁移 → 停止，查 `erp.alembic_version_erp` 的变更来源后再决策。
- 耗时：约 1 min。

### 4.7 六项汇总表

| # | 数字 | 当前状态 | 通过标准 |
|---|---|---|---|
| 1 | `/healthz` | ✅ 可执行 | 200 ×3 |
| 2 | `job_queue` 待处理数 | 🔴 R5 | 交付后：不单调增长 + 有完成迁移 |
| 3 | `worker_heartbeat` 最新时间 | 🔴 R6 | 交付后：非空且 age < 300s |
| 4 | `/metrics` 公网不可达 | 🔴 R4 | 连接失败 / 403 / 404 |
| 5 | 备份 `SHA256SUMS` | ✅ 可执行 | 全部 OK |
| 6 | `alembic_version_erp` | ✅ 可执行 | 等于镜像 `heads`，且与 RELEASES 登记一致 |

---

## 5. T+30 → T+2h：观察期

### G16 每 15 分钟巡检 ✅（每次约 2 min）

```bash
# cwd: 服务器任意目录
docker compose -f /opt/smart-erp/docker-compose.yml --env-file /opt/smart-erp/.env --profile demo ps
docker logs --since 15m erp_api 2>&1 | grep -cE ' (ERROR|CRITICAL) ' || true
curl -s -o /dev/null -w 'api=%{http_code}\n' http://127.0.0.1:8010/healthz
curl -s -o /dev/null -w 'web=%{http_code}\n' http://127.0.0.1:8088/healthz
```

- **预期**：三容器始终 `healthy`；ERROR 行数为 0 或与基线持平；api 恒 200。
- **失败判定/动作**：任一容器 `unhealthy` 超过 5 min → §6；ERROR 突增 → 先抓 `docker logs --since 30m erp_api > /tmp/erp-api-$(date +%H%M).log` 留证再决策。
- **耗时**：每次 2 min，共 8 次。

### G16b 迁移版本冻结巡检（发布含新迁移时）✅（每次约 30s）

```bash
# cwd: /opt/smart-erp
set -a; . .env; set +a
docker run --rm --network "${ERP_DB_NETWORK:-hulkai-gz-api_default}" postgres:15-alpine \
  psql "$(printf '%s' "$ERP_DATABASE_URL" | sed -E 's#^postgresql\+asyncpg:#postgresql:#')" -Atc \
  "select max(version_num) from erp.alembic_version_erp;"
```

- **预期**：与 T+10（§4.6）记录的值**逐字相同**；观察期内不得出现新的 `version_num`。
- **失败判定/动作**：值变了 → 窗口内有人手工跑迁移或自动迁移被误开：立即停止其他变更，比对 `.meta.txt` 与 `RELEASES.md` 的登记值；版本对不上就走 §6.3，**不要**继续观察期。
- **耗时**：30s/次，随 G16 同频执行。

### G17 常驻进程巡检（8011 探针 / scheduler lease）🔴 R7 R8（未交付/本版不发）

> **当前不可执行**：
> - `R7` 8011 worker 探针：本版 demo 不起 `worker`，容器内 `http://127.0.0.1:8011/healthz` 无监听，探针必然失败；
> - `R8` scheduler 巡检：本版 demo 不起 `scheduler`，`erp.scheduler_lease` 为空属正常，不能据此判故障。
>
> 交付后启用：`docker exec erp_worker python -m tools.health worker`（healthy 时 exit 0，stale 时非 0）、
> `docker exec erp_scheduler python -m tools.health scheduler`，并按 §4.3 的 SQL 复核心跳行。

### G18 观察期结束判定 ✅（约 5 min）

全部满足才宣布上线完成：

1. 三容器连续 90 min `healthy`；
2. `/healthz` 连续 200，无 > 60s 的中断；
3. 无新增 P1（`/erp/api/*` 偶发 502 不计，见 RUNBOOK §6）；
4. 6 个数字中"当前可执行"的 3 项全过（`/healthz`、备份 `SHA256SUMS`、`alembic_version_erp`），三个 🔴 项按 §7 登记为遗留而非"通过"；
5. `erp.alembic_version_erp` 观察期内未变化（G16b 逐次一致）；
6. 台账追加一条：`NEW_SHA`、备份目录、6 数字快照（含迁移版本）、红项清单。

---

## 6. 回滚

### 6.1 决策树

| 现象 | 判断 | 动作 |
|---|---|---|
| 容器起不来 / 5xx 持续 | 代码或配置问题 | §6.2 镜像回退 |
| 迁移执行到一半失败 | 迁移问题 | 停写 → §6.3 备份恢复 |
| 数据被写坏 / 对账结果异常 | 数据问题 | 停写 → §6.3 备份恢复 |
| 白屏、资源 404 | 构建产物问题 | §6.2 镜像回退（并核对 `ERP_WEB_BASE`） |

### 6.2 镜像回退（首选，约 2–5 min）

```bash
# cwd: /opt/smart-erp
ERP_IMAGE_TAG=<上一版 sha> docker compose -f docker-compose.yml --env-file .env \
  --profile demo up -d --remove-orphans
```

- **适用**：普通迁移（加表/加列/加索引）、纯代码缺陷、静态产物问题。
- **数据边界**：**只回代码，不回数据库**。库结构保持新版本（向前兼容），新版本写入的数据仍在，旧代码忽略不认识的新列 → **不丢数据**。
- **不适用**：破坏性迁移（删列/改类型/搬数据）——旧代码可能读不到已变的列。
- **回退后核对**：`docker port erp_api` = `127.0.0.1:8010`；`/healthz` 200；`docker inspect erp_api --format '{{.Config.Image}}'` 为旧 sha。

### 6.3 从备份恢复（兜底，约 20–60 min）

**前提：迁移不可自动回退。** 镜像回退救不了库，只有备份能。

```bash
# ① 停写（先别让坏状态继续落库）
docker compose -f /opt/smart-erp/docker-compose.yml --env-file /opt/smart-erp/.env \
  --profile demo stop api connector-gw web

# ② 先恢复到演练库验证（不碰生产）
scripts/restore-smart-erp.sh \
  --dump /opt/smart-erp/backups/<日期>/smart_erp-<时间戳>-<sha12>.dump \
  --target-url 'postgresql+asyncpg://erp_migrator:***@hulkai_aps_postgres:5432/smart_erp_restore' \
  --objects /opt/smart-erp/backups/<日期>/erp_objects-<时间戳>-<sha12>.tar.gz \
  --target-volume smart-erp_objects_restore
# 核对脚本打印的表数 / alembic 版本与 .meta.txt 一致

# ③ 确认后再覆盖生产（--force 的真实语义见 RUNBOOK §2.1）
scripts/restore-smart-erp.sh \
  --dump /opt/smart-erp/backups/<日期>/smart_erp-<时间戳>-<sha12>.dump \
  --target-url 'postgresql+asyncpg://erp_migrator:***@hulkai_aps_postgres:5432/smart_erp' \
  --objects /opt/smart-erp/backups/<日期>/erp_objects-<时间戳>-<sha12>.tar.gz \
  --target-volume smart-erp_objects --force

# ④ 起回旧版本
ERP_IMAGE_TAG=<上一版 sha> docker compose -f /opt/smart-erp/docker-compose.yml \
  --env-file /opt/smart-erp/.env --profile demo up -d api connector-gw web
```

- **适用**：破坏性迁移、数据被写坏、`--force` 差异校验必须走到 exit 0。
- **数据边界**：**丢失 = 备份时刻之后的所有写入**（demo 档无 PITR，RPO 由备份频率决定；用发布前那份备份时，丢失窗口 ≈ 发布开始到现在）。恢复必须先停写，否则恢复期间新写入会被再次覆盖。
- **`--force` 的坑**：它只 `--clean --if-exists` 备份包里出现过的对象，**不是整库重置**；恢复后若打印"库内 − 备份包"非空并以 **exit 5** 退出，先手工核对并 `DROP` 旧对象，再用同一备份重跑，不要忽略这一行。
- **禁止**：在生产库直接 `alembic downgrade`；要试先在 `smart_erp_restore` 演练库跑。

### 6.4 回滚后必须复核

1. `docker port erp_api` = `127.0.0.1:8010`；
2. `/healthz` 200、`https://<域名>/erp/api/healthz` 200；
3. `erp` 表数、`alembic` 版本与恢复目标一致；
4. 备份文件、回滚时间点、新旧 sha 全部写进台账。

---

## 7. 🔴 当前不可执行清单（9 条）

| 编号 | 步骤 | 依赖 / 原因 | 责任方 | 何时解禁 |
|---|---|---|---|---|
| R1 | G05 备份异地拷贝 | 客户侧备份机与通道 | 客户侧 + S8 | 客户提供后 |
| R2 | G06 安全组核对收口 | 云控制台人工，脚本无法代做 | 运维（人工） | 上线日人工执行；**公网 IP 机器上此项不完成不得上线** |
| R3 | G07 hk-web nginx 片段 | 不在本仓库，需 hk-web 维护人 | hk-web | 上线前人工落地 |
| R4 | G14 / 4.4 公网链路与 `/metrics` 不可达 | 依赖 R2 + R3 | 运维（人工） | R2/R3 完成后 |
| R5 | 4.2 `job_queue` 待处理数 | worker 常驻进程未交付（B-S8-01） | S3 | B-S8-01 解除并 S8 复验 |
| R6 | 4.3 `worker_heartbeat` 最新时间 | 同上 | S3 | 同上 |
| R7 | 8011 worker 探针 | 同上 | S3 | 同上 |
| R8 | G17 scheduler 巡检 | scheduler 未交付，本版 demo 不起 | S3 | 同上 |
| R9 | full 档全部步骤（`api-prod` / `migrate` / `doc` / `worker-*`） | B-S8-01 未解除，`RELEASES.md` 准入条款第 2 条禁发 | S3 + S8 复验 | B-S8-01 解除后 |

> 红项一律**不当作"通过"**：上线报告里写"未执行（原因）"，不写"跳过"。
