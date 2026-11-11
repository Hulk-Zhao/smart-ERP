# smart-ERP 运维手册（备份 / 恢复 / 升级）

> 上线当天按 [GO-LIVE-RUNBOOK.md](./GO-LIVE-RUNBOOK.md) 逐条执行（按时间轴排列、含预期输出与回滚动作）；
> 本文件是背景、细则与演练结论。

> 适用：共用 PG15 实例上的 `smart_erp` 库（`erp` schema）与 `erp_objects` 对象卷。
> 红线：**不备份、不恢复、不 DROP APS 的任何对象**；所有操作只在自己这一户里。
> 脚本：`scripts/backup-smart-erp.sh`、`scripts/restore-smart-erp.sh`、`scripts/upgrade-smart-erp.sh`。

## 0. 首次初始化：先 `db/00-provision.sql`，再 `alembic upgrade head`

> **顺序不可颠倒，第 1 步不能省。** `deploy/smart-erp/db/00-provision.sql` 是
> `erp_migrator` / `erp_app` 两个角色、`erp` schema 与默认权限的**唯一**来源；
> `db/migrations/` 对 `ticket`、`job_queue`、`compensation_task` 等业务表
> **一条 `GRANT` 都没有**（只有部分审计 / 运行期表带收紧后的局部授权）。

1. **先**执行 `db/00-provision.sql`：建角色、建 `erp` schema，并设置
   `ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA erp`（表 4 项、序列、函数）。
2. **再**以 `erp_migrator` 执行 `alembic upgrade head`。
   反过来做，`ALTER DEFAULT PRIVILEGES` 只对**之后**创建的对象生效，迁移建出来的表不会
   自动授予应用角色，而迁移里又没有对应 `GRANT` 兜底。

**跳过第 1 步的表现（不会当场报错）**：库能迁、服务能起、`/healthz` 可能返回 200，
但应用对 `ticket` / `job_queue` / `compensation_task` 等表没有写权限，直到第一次写入
才报 `permission denied for table ...`——排查方向容易被带到业务代码上。

**扩展是可选前置（vector 缺失不阻断）**：`00-provision.sql` 对 `pg_trgm` / `pgcrypto`
是硬要求；对 `pgvector` 只在实例可用时创建，缺失时 `RAISE WARNING` 跳过，脚本继续。
没有 vector 时**不受影响**：对账、工单、审批、补偿、审计、计量、批次列表；
**不可用**：向量检索 / 相似度匹配（当前代码没有任何依赖，`grep -r "vector"` 只命中建库脚本）。
后续若要启用向量能力，需实例 owner 以 SUPERUSER 手工执行
`CREATE EXTENSION vector SCHEMA erp;` 后重启服务，再按功能开关上线。

**迁移完成后自检**（`psql` 直接粘贴；grantee 用应用角色：生产 `erp_app`，CI 改 `erp_test`）：

```sql
SELECT c.relname AS table_name,
       coalesce(string_agg(DISTINCT g.privilege_type, ',' ORDER BY g.privilege_type), '(none)')
         AS privileges
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN information_schema.role_table_grants g
         ON g.table_schema = 'erp'
        AND g.table_name = c.relname
        AND g.grantee = 'erp_app'      -- CI 改成 erp_test
 WHERE n.nspname = 'erp' AND c.relkind = 'r'
 GROUP BY c.relname
 ORDER BY c.relname;
-- 期望：应用直接读写的业务表各自含 DELETE,INSERT,SELECT,UPDATE；
-- 以当前 migration head 为准：表清单会随迁移变化，本条只验权限，不锁表名与数量。
-- 出现 (none) 就是第 1 步被跳过（或对象在 provision 之前创建），先补授权再继续。
-- audit_* / recon_* / worker_* 等只有收紧后的局部权限（如仅 INSERT,SELECT），属设计如此。
```

角色不要混：生产 / 网关是 `erp_app`，迁移是 `erp_migrator`，CI 是 `erp_test`。

## 1. 备份

### 1.1 什么时候必须备份

| 时机 | 是否强制 | 说明 |
|---|---|---|
| 每次发布/升级前 | **强制** | `upgrade-smart-erp.sh` 会检查当天是否已有备份，没有就拒绝执行 |
| 执行任何迁移前 | **强制** | 迁移不可自动回退，备份是唯一兜底 |
| 手工改数据/修数前 | **强制** | 先 `backup-smart-erp.sh`，产物留在当天目录 |
| 每周例行 | 是 | 与每日检查合并；发现失败当天补跑 |
| 实例 owner 变更共用 PG 配置前 | 是 | 我们只备自己，实例级变更由 owner 负责 |

### 1.2 保留几份、在哪存

- **7 份日备**：`<server>/opt/smart-erp/backups/<yyyy-mm-dd>/`，保留 7 天；
- **4 份周备**：每周五的备份额外复制到 `weekly/`，保留 4 周；
- **每次发布前的 1 份**：命名里带 `ERP_IMAGE_TAG` 前 12 位，随版本保留 30 天；
- **异地一份**：每周把最新周备 `scp` 到备份机（与业务机不同机器；无异地则视为不达标）；
- 备份目录不放在 `erp_objects` 卷里（同盘同毁），放宿主机独立目录。

### 1.3 执行

```bash
ERP_IMAGE_TAG=$(git rev-parse HEAD) \
ERP_BACKUP_DATABASE_URL='postgresql+asyncpg://erp_migrator:***@hulkai_aps_postgres:5432/smart_erp' \
scripts/backup-smart-erp.sh --out /opt/smart-erp/backups
```

产物（同目录 `SHA256SUMS` 追加）：

```
smart_erp-<时间戳>-<tag前12位>.dump      # pg_dump -Fc，单库
erp_objects-<时间戳>-<tag前12位>.tar.gz   # erp_objects 卷
smart_erp-<时间戳>-<tag前12位>.meta.txt   # 表数、alembic 版本，恢复时比对
```

脚本会先校验库名**必须等于 `smart_erp`**，否则直接退出（exit 3），从源头防止把 APS 备进来。

### 1.4 校验

```bash
cd /opt/smart-erp/backups/<日期>
sha256sum -c SHA256SUMS
pg_restore -l smart_erp-*.dump | head -30   # 只列目录，不连库
```

### 1.5 文件通道收件箱（inbox）不参与备份

`deploy/smart-erp/docker-compose.yml` 给 connector-gw 挂了 `./inbox:/data/inbox:ro`（相对部署目录），
对应宿主 `<部署目录>/inbox`。它和 `erp_objects` 卷是两回事，备份口径如下：

- **不进备份**：inbox 里是供应商对账单/送货单等**瞬时输入**，导完就应归档或删除；
  它也不在任何 named volume 里，`backup-smart-erp.sh` 只备 `smart_erp` 库与 `erp_objects` 卷，
  **不要**为了让「备份更全」手工把 inbox 打进 tar——那会把未处理的原始单据复制到备份机上；
- **恢复不清空**：restore 只动库与 `erp_objects` 卷，**不得** `rm -rf inbox`；恢复后待处理的文件仍在那里，
  重复导入由业务幂等键去重，不靠删文件；
- **目录权限**：`deploy-images.sh` 在上传前执行
  `install -d -m 0750 -o 10001 -g 10001 <部署目录>/inbox`（10001 = 网关容器用户 app）。
  权限给 0777 或把属主改成 root 都会让 connector-gw 读不到并**启动失败**（不静默降级）；
- **取数方式**：演示时把 CSV 放进 `<部署目录>/inbox` 即可，网关以只读方式扫描；容器内只能读、不能写。
  demo 档默认开启（`env/demo.env` 里 `ERP_FILE_INBOX=/data/inbox`）；full/prod 保持 `ERP_FILE_INBOX=` 空值 = 关闭。

## 2. 恢复

### 2.1 硬规则

1. 目标库名必须以 `smart_erp` 开头（演练用 `smart_erp_restore`；**不允许**指向 APS 库）；
2. 目标库非空 → 默认拒绝（exit 4），必须显式 `--force`；
3. **`--force` ≠ 整库重置**：它只是给 `pg_restore` 加 `--clean --if-exists`，清掉的是
   **备份包 TOC 里出现过的对象**。备份包里没有的旧表、旧序列、旧外键会原样留下，
   形成「半新半旧」的库。脚本恢复后会做差集校验（§2.2），差集非空以 exit 5 拒绝通过；
   真要整库重置，只能由实例 owner 另做 `DROP DATABASE / CREATE DATABASE`，恢复脚本不做；
4. 恢复后**不自动跑迁移**：先比对 `.meta.txt` 的 `alembic_revision`，再按第 3 节决定是否升级。

### 2.2 操作

```bash
# 演练/新库
scripts/restore-smart-erp.sh \
  --dump backups/2026-11-11/smart_erp-2026-11-11T003000-<sha12>.dump \
  --target-url 'postgresql+asyncpg://erp_migrator:***@hulkai_aps_postgres:5432/smart_erp_restore'

# 覆盖已有库（谨慎）
scripts/restore-smart-erp.sh --dump <file> --target-url <url> --force

# 连对象卷一起恢复
scripts/restore-smart-erp.sh --dump <file> --target-url <url> \
  --objects backups/2026-11-11/erp_objects-...tar.gz --target-volume smart-erp_objects_restore
```

恢复后脚本会打印 `erp` 表数与 `alembic` 版本；与 `.meta.txt` 不一致就停下来查，不要接着升级。

随后脚本自动做一次对象集合校验：把 `pg_restore -l` 里用户 schema（`erp` / `public`）的
表/序列/视图/物化视图/外部表/外键，与恢复库里 `pg_class` / `pg_constraint` 的实际对象做差集。

- **库内 − 备份包 ≠ 空**：说明 `--force` 留下了备份里没有的旧对象，打印 `+ 旧对象` 清单并以
  **exit 5** 退出。确认可删后手工 `DROP`，再用同一备份重跑；不要忽略这行；
- **备份包 − 库内 ≠ 空**：`pg_restore` 没恢复完整，同样 exit 5，先查 stderr 再用新库重来。

### 2.3 演练结论（最近一次，2026-11-11 实测）

演练方式：本机临时 `postgres:15-alpine` 容器（`s8drill-pg`，PG 15.18）+ 真实备份包，不碰生产。

```
备份：  pg_dump -Fc smart_erp → 40,296 B；erp_objects tar.gz → 191 B
        meta: erp_table_count=14  alembic_revision=0002_s1s3s4_merge
        sha256sum -c SHA256SUMS → 3 个产物全部 OK
守卫：  smart_erp_restore 先建 1 张 public.drill_blocker 表
        不带 --force → ERROR: 目标库已有 1 张用户表，拒绝覆盖；restore_guard_exit=4
恢复：  --force --objects … --target-volume s8drill_objects_restore
        pg_restore --single-transaction --exit-on-error --clean --if-exists
        核对：erp schema 表数=14；alembic 版本=0002_s1s3s4_merge；restore_force_exit=0
校验：  恢复库 erp.job_queue 标记行=1；alembic upgrade head 无 "Running upgrade"（已在 head）
对象：  恢复卷文件 2 个，sha256 与源卷逐字节一致
```

结论：表数、迁移版本、数据行、对象文件四项全部一致；非空库默认拒绝生效（exit 4）。

### 2.4 一致性校验演练（2026-11-11 追加，head 已到 `0003_recon_persistence`）

```
库：  20 张 erp 表；备份包对象 37（表/序列/视图/外键）
造脏：目标库先建 public.legacy_table / legacy_seq / legacy_child(带 FK)
恢复：--force 成功，但差集非空 → 打印 4 个旧对象 → consistency_fail_exit=5
  + FK public.legacy_child legacy_child_pid_fkey
  + SEQUENCE public.legacy_seq
  + TABLE public.legacy_child
  + TABLE public.legacy_table
清理：手工 DROP 三个旧对象后重跑同一备份
结果：备份包对象=37；恢复库对象=37；一致性校验通过；consistency_pass_exit=0
数据：erp.job_queue 标记行=1；alembic 版本=0003_recon_persistence
```

说明：`erp` schema 因在备份包内，`--clean` 会先 `DROP SCHEMA`，旧对象会导致恢复直接失败；
真正会「静默留下」的是 `public` 等不在包内的 schema——上面的演练覆盖的正是这一类。

## 3. 升级与回滚

### 3.1 升级

```bash
# 0) 先备份（脚本也会检查）
ERP_IMAGE_TAG=<new-sha> scripts/backup-smart-erp.sh --out /opt/smart-erp/backups

# 1) 升级（旧 tag 必须本机已有镜像，否则拒绝开始）
scripts/upgrade-smart-erp.sh --old-tag <old-sha> --new-tag <new-sha> --profile demo
```

顺序固定为：**migrate 一次性服务 → 应用容器 → `/healthz` 200**。
migrate 失败时应用容器不动，仍跑旧版本；健康检查失败默认只打印回滚命令，加 `--auto-rollback` 才自动回滚应用容器。

### 3.2 回滚

```bash
ERP_IMAGE_TAG=<old-sha> docker compose --env-file deploy/smart-erp/.env \
  --profile demo up -d --remove-orphans
```

**迁移不可自动回退**：回滚镜像不会把数据库降级。

- 普通迁移（加表/加列/加索引）：回旧镜像即可，库结构向前兼容；
- 破坏性迁移（删列/改类型/搬数据）：先停写，评估 `alembic downgrade` 的影响面；
  没有把握就从**发布前那份备份**恢复到新库，核对表数与版本后再切；
- 任何情况下不要在生产库上试 `downgrade`，先在 `smart_erp_restore` 演练库跑一遍。

### 3.3 演练结论（最近一次，2026-11-11 实测）

本机真实起容器完成，临时 PG15 容器 + compose demo 档：

```
成功：  bbbb…（旧）→ cccc…（新）upgrade_exit=0
        migrate 以一次性容器 smart-erp-migrate-run-* 运行（alembic 在 migrate 服务里跑）
        api /healthz → 200；erp_api image=smart-erp/app:cccc…
迁移失败：cccc… → dddd…（app 镜像无 alembic）
        ERROR: migrate 失败（exit=1）。应用容器未动，仍跑在 cccc…
        upgrade_exit=5，并打印回滚命令 ERP_IMAGE_TAG=cccc… docker compose … up -d
        实测 erp_api 仍是 cccc… 且 /healthz=200
健康失败：--auto-rollback 且探针指向未监听端口
        upgrade_exit=6 → 自动用 cccc… 重建；复查 image=cccc…、/healthz=200
```

注意：升级成功/失败全程都会打印「迁移不可自动回退」；回滚只回镜像，数据库不回退。

## 4. 出事时的顺序

1. 停写（`docker compose stop api worker scheduler`），别让坏状态继续落库；
2. 判断是代码问题还是数据/迁移问题；
3. 代码问题 → 回旧 sha；数据/迁移问题 → 从最近备份恢复到 `smart_erp_restore` 验证；
4. 恢复生产前，先在本机/演练库跑 `restore-smart-erp.sh` + `alembic upgrade head`；
5. 全过程记录到台账（时间、sha、备份文件、表数、版本）。

## 5. 公网入口与端口暴露（8010 不得对公网开放）

### 5.1 唯一公网入口

公网入口**只有 hk-web 的 nginx**。广州侧 api 只监听宿主回环地址，路径转发关系：

```
公网 → hk-web nginx → /erp/api/ → 127.0.0.1:8010（广州宿主）→ erp_api 容器
```

- 生产必须用 `--profile full` 启动：该档的 `api-prod` 端口绑定是
  `127.0.0.1:${ERP_API_PORT:-8010}:8010`，公网不可达；
- `--profile demo` 的 `api` 仍是 `0.0.0.0:8010`，**仅本地/演示，公网部署禁止**
  （S7 实测：安全组漏一条，`/metrics` 与全部内部接口直接裸奔）；
- 8011（worker 指标）、8012（connector-gw）、8020（doc）一律只 `expose` 在
  `smart-erp_net` 内网，任何档位都不发布到宿主；
- 8088（web 静态站点）只允许香港反代访问，同样不在安全组对 `0.0.0.0/0` 放行。

切换档位（例如本机 demo → 服务器 full）必须先 `docker compose --profile demo down`：
两档的 api 共用 `container_name: erp_api`，直接跨档 `up` 会撞容器名与 8010 端口。

### 5.2 hk-web nginx 必配片段

> **这是 hk-web 上的人工步骤，不在本仓库**：仓库里没有 nginx 配置，下面片段由运维手工落地并 reload；
> 改完之后不要以为已经配好了，必须按 5.3 的验收口径实测。

```nginx
# 指标有两个暴露面，必须都拦（deny 或直接 404，二选一）：
#
# ① /erp/api/metrics —— App 指标的真实公网路径。
#    nginx 必须剥掉 /erp/api/ 前缀再转给 api（见下方 proxy_pass 结尾的 "/"）。
#    api 以 uvicorn --root-path=/erp/api 启动，应用内只认无前缀路径（/healthz、/metrics），
#    所以容器内真实端点是 /metrics，公网路径由本 location 命中后拒绝。
#    只写 location = /metrics 拦不住它（历史写法就是漏在这里）。
# ② /metrics —— 站点根路径上的同名路径，防御性兜底（不是本应用的转发路径，
#    同机其它站点或默认 server 若暴露 /metrics 会被它挡住）。
location = /erp/api/metrics {
    deny all;          # 只接受 deny all（403）；不要写 return 404（见下方验收矩阵）
}
location = /metrics {
    deny all;          # 只接受 deny all（403）；不要写 return 404（见下方验收矩阵）
}

# 业务 API 反代到广州宿主回环地址；上游写字面量时，proxy_pass 结尾**必须带 "/"**（剥掉 /erp/api/ 前缀）。
# 机制：uvicorn 把 root_path 拼到 scope["path"] 前，Starlette 路由匹配时再剥一次，
# 净效果是应用只认无前缀路径；保留前缀会让所有 /erp/api/* 返回 404。
# --root-path 只用于 docs/openapi 的 server 地址，不能既保留前缀又开 root-path。
location /erp/api/ {
    proxy_pass http://127.0.0.1:8010/;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}
```

> **禁止写法（2026-11-11 真容器实测）**：不要为了「请求时解析上游」把地址抽成变量再带 URI，
> 例如 `set $erp_api_upstream http://api:8010;` + `proxy_pass $erp_api_upstream/;`。
> nginx 在 `proxy_pass` 使用变量时**不做 location 前缀替换**，URI 会按指令原样发送，
> 该 location 下的所有请求都被压成这个 URI，全部 404（与后端是否开 `--root-path` 无关）。
> 若确需保留变量，必须写成 `proxy_pass $erp_api_upstream;`（不带 URI）+
> `rewrite ^/erp/api/(.*)$ /$1 break;`；`(.*)` 空匹配时 `/erp/api/` 会变成 `/`，
> 不会产生空 URI。
>
> **也不要为了剥前缀改回字面量上游**（2026-11-11 CI 事故）：字面量
> `proxy_pass http://api:8010/;` 会让 nginx 在启动时用系统解析器解析 `api`，
> `resolver` 上的 `ipv6=off` 随之失效——Docker DNS 同时返回 A/AAAA 时 nginx 可能
> 选中 AAAA，而 api 只监听 IPv4，结果是 502；api 尚不可解析时更会直接
> `host not found in upstream "api"` 退出。
>
> 因此仓库内 `web-nginx.conf`（**仅本机预览/compose demo 使用**）保留
> `resolver 127.0.0.11 valid=10s ipv6=off;` + 变量式 `proxy_pass $erp_api_upstream;`
> + 上面的 `rewrite`。`tests/deploy` 有两层回归：静态断言禁止「变量 + URI」并要求
> resolver 带 `ipv6=off`；真容器用例在 CI deploy-static 里用 `nginx -T` 校验运行中
> 配置，并断言「web 经 Docker DNS 解析到的 api 地址族 == api 实际监听的地址族」，
> 再直打 `/erp/api/healthz`、`/erp/api/openapi.json`。
>
> 云上路径不受这条影响：公网 → hk-web nginx → `proxy_pass http://127.0.0.1:8010/;`
> → 广州宿主 erp_api，不经过 web 容器。

**指标验收口径（三行矩阵，按 1 → 2 → 3 顺序执行，缺一行都不算通过）：**

> 判断原则：**只有第 2 行（开启后容器内直连 200）能证明端点存在，第 3 行才有意义。**
> 跳过第 2 行直接看第 3 行，等于在验一个可能不存在的端点——`#48` 的 deny 规则
> 就是这样保护了一个不存在的端点，靠 404 通过，属于假通过。

> 探针与 nginx 的 deny 必须用同一个路径；改了 `ERP_METRICS_PATH` 就同步替换下面的
> `/metrics` 和 nginx 片段里的路径。

容器内探针（app 镜像自带 python，不依赖镜像里有没有 curl）：

> **前提：转发必须已剥掉 `/erp/api/` 前缀**（`proxy_pass .../;`）。若沿用旧写法保留前缀，
> `/erp/api/metrics` 会因应用只认 `/metrics` 而返回 404——**这种 404 不是拦截生效的证据**。
> 必须先验 `/erp/api/healthz` 为 200（证明前缀通路与剥前缀都正确），再看 metrics 的 403/404。

```bash
probe() {
  docker exec -T erp_api python -c "
import urllib.request, urllib.error
try:
    r = urllib.request.urlopen('http://127.0.0.1:8010/metrics', timeout=5)
    print(r.status)
    print(r.read().decode()[:2000])
except urllib.error.HTTPError as e:
    print(e.code)
"
}
```

| # | 操作 | 期望 | 证明了什么 |
|---|---|---|---|
| 1 | `deploy/smart-erp/.env` 里 `ERP_METRICS_ENABLED=`（空）或 `0`，`docker compose --env-file deploy/smart-erp/.env --profile full up -d --force-recreate api` 后执行 `probe` | **404** | 关闭时端点不暴露 |
| 2 | `deploy/smart-erp/.env` 里 `ERP_METRICS_ENABLED=1`（`true/yes/on` 等价），同样重建后执行 `probe` | **200**，且正文含 `smart_erp_recon_batches_total` | 端点确实挂载、指标在产出 |
| 3 | 保持第 2 行的开启状态，从外网访问 | **403**（`curl -m 5 -o /dev/null -sS -w '%{http_code}\n' https://<域名>/erp/api/metrics`） | nginx 的 deny 生效，而不是「端点不存在」 |

第 3 行返回 **404 不算通过**：它与第 1 行同码，既可能是 nginx 写成了 `return 404` 而不是
`deny all`，也可能是配置没 reload。先改配置再重测；返回 200 即指标外泄，发布不通过。

站点根路径兜底仍需单独验（它不是应用的转发路径，漏配时同样会 200）：

```bash
curl -m 5 -o /dev/null -sS -w '%{http_code}\n' https://<域名>/metrics   # 期望 403
```

配合 5.3 第 ③ 条（`http://<公网IP>:8010/metrics` 必须连接失败），「矩阵三行 + 根路径兜底 +
公网直连失败」全部通过，才算「指标既不能绕过 nginx，也不能直连宿主」。换域名/改前缀后必须重跑。

> **接线与开关下发位置**：
>
> - 挂载点在 `apps/main.py:35`：`install_metrics(app, get_container().metrics)`（#65）。
>   指标注册表取的是容器里同一个 `metrics` 实例，业务埋点与 `/metrics` 输出是同一份数据。
> - `ERP_METRICS_ENABLED` 只认 `1/true/yes/on`（大小写不敏感），空值或其它值一律视为关闭；
>   关闭时 `install_metrics` 直接返回、不注册路由，所以第 1 行是 404。
>   `docker-compose.yml` 已把该键显式下发给 `api`（schema `scope=compose`、`services=["api"]`），
>   `deploy/smart-erp/.env.example` 保持空值——生产默认关闭。
> - 端点路径由 `ERP_METRICS_PATH` 决定（默认 `/metrics`），同样由 compose 下发给 `api`。
>   改这个值必须同步改探针 URL 和 nginx 的 `location = /erp/api/metrics`；且它**必须以 `/`
>   开头**，否则启动期 `install_metrics` 抛 `MetricError`，api 容器直接起不来——不是运行期 404，
>   而是 `docker compose ps` 里 restart 计数一路涨。
> - 做第 2 行时在 `deploy/smart-erp/.env` 里临时写 `ERP_METRICS_ENABLED=1`，验收完删掉该行
>   （或改回 `0`）并再次 `--force-recreate api`；`env/prod.env` 不登记该键，避免「关闭」
>   在线上偶发变成「开启」。
> - 镜像口径：以上按 #65 的装配实现写。若镜像主干 sha 早于 #65 合入（例如 `c3c662b`），
>   `apps/main.py` 里还没有这行调用，第 2 行会恒为 404——那是镜像内容问题，不是开关没生效。

### 5.3 发布后自检（三条都要过）

```bash
# ① 容器端口绑定必须是 127.0.0.1，不能是 0.0.0.0
docker port erp_api
# 期望：8010/tcp -> 127.0.0.1:8010

# ② 宿主监听地址同理（0.0.0.0:8010 或 *:8010 即视为不合格）
ss -lntp | grep ':8010'

# ③ 从公网侧验证不可达（在外部机器执行，必须超时/拒绝）
curl -m 3 -sS http://<公网IP>:8010/healthz   # 期望：连接失败
curl -m 3 -sS http://<公网IP>:8010/metrics   # 期望：连接失败
```

安全组入站规则中 **8010 不得出现 `0.0.0.0/0` / `::/0`**；只允许香港反代机的
固定出口 IP（若走公网串联），或干脆不开放、走内网/专线。

## 6. web 冷启动 502 与可用性探针

`web` 不依赖 `api`（compose 的 `depends_on` 无法按 profile 条件化，见 §5.1）。因此：

- **api 尚未就绪时**，`/erp/api/*` 返回 502/504，静态页面本身正常；api 健康后 nginx
  会自动恢复转发，**不需要重启 web**；
- 这是冷启动的正常窗口（api `start_period` 60s），**不是故障**：首发监控只应告警
  `/healthz` 连续失败，不要把「上线后 60s 内的少量 502」当 P1；
- `web` 自身的探针是容器内 `wget http://127.0.0.1:8080/healthz`（nginx 直接返回 200，
  不依赖 api），所以 web 的健康状态与 api 可用性是**两条独立指标**：
  web 探针失败 = nginx/静态资源问题；web 探针正常但 `/erp/api/` 502 = api 未就绪。
- 验收口径：发布后先看 `erp_api` 的 `/healthz` 200，再看公网 `/erp/api/healthz`；
  两者都过才认为升级完成，期间 502 不计入故障时长。

## 7. 监控与告警（仅 full 档）

`prometheus` 与 `alertmanager` 只挂在 `full` profile；`demo` 档刻意不装评估器，保持 1200MiB 预算。
在 demo 上只能写「规则已定义、无评估器」，**不得写「已告警」**。

**抓取目标**（都在 compose 内网，不发布宿主端口）：

| job | 地址 | 说明 |
|---|---|---|
| `erp-api` | `api:8010/metrics` | 写与暴露同进程（`apps/main.py`） |
| `erp-scheduler` | `scheduler:8011/metrics` | 审计链 P0 指标；端点由 `core/runtime/metrics_server.py` 提供 |
| `erp-compensation` | `compensation:8013/metrics` | 补偿积压 / 到期 / 未来重试 / 时钟偏移 / unresolved |

> **抓取一律直连容器端口，不经过任何 nginx。** `api:8010` / `scheduler:8011` / `compensation:8013`
> 都是 compose 内网的服务名 + 端口，Prometheus 直接连；`web-nginx.conf` 里对
> `/erp/api/metrics` 的 404 只作用于**本机预览入口（web:8080）**，hk-web 的 `deny all`
> 只作用于公网路径，两者都不影响采集。**不要**为了「统一入口」把抓取改成经 nginx 取
> `/erp/api/metrics`——那样会拿到 404，且该 404 不是指标关闭的证据。

规则文件是**直接挂载**的 `observability/alerts.rules.yaml`（不复制），改完规则 `docker compose restart prometheus` 即生效。

### 7.1 本期不接外部通知渠道（明确取舍，不是遗漏）

`alertmanager.yml` 的 receiver `erp-unrouted` 是**空接收器**：告警会被接收、去重、可用 API 查询，
但**不会发给任何人**——没有邮件、没有飞书、没有短信、没有 webhook。

因此：

- 值班方式**只有一种**：人看 firing 面板（`Prometheus /alerts` 或 Alertmanager UI）。
- 任何验收、发布记录、看板、周报**不得写「已告警」「已通知」「已触达值班」**；
  只能说「已进入 Alertmanager，receiver 为空，无人接收」。
- 评估器本身掉线由 `SmartErpScrapeTargetDown` 兜住，但它同样只到 Alertmanager。

**将来接通道时的最低要求**：改 `alertmanager.yml` 加真实 receiver 后，必须用**接收端的实际记录**
证明送达（邮件到达、群消息出现、webhook 服务端日志），Alertmanager 页面显示 `sent` 不算证据；
并在本节替换掉「本期不接外部通知渠道」这段，写明接收人与升级路径。

### 7.2 自检命令

```bash
# 抓取目标是否都 up（应看到 3 个 target、health=up）
docker compose -f deploy/smart-erp/docker-compose.yml --profile full exec prometheus \
  wget -qO- 'http://localhost:9090/api/v1/targets?state=active'

# 规则当前状态（inactive / pending / firing）
docker compose -f deploy/smart-erp/docker-compose.yml --profile full exec prometheus \
  wget -qO- 'http://localhost:9090/api/v1/rules?type=alert'

# Alertmanager 实际收到的条目
docker compose -f deploy/smart-erp/docker-compose.yml --profile full exec alertmanager \
  wget -qO- 'http://localhost:9093/api/v2/alerts'
```

**「会响」的唯一证据**是规则状态从 `inactive` → `pending` → `firing`，并在 `/api/v2/alerts`
里出现对应条目；只贴 YAML 或 `promtool` 的 `SUCCESS` 不算验证。

> 2026-11-11 实测（full 档同等配置）：篡改一条 `erp.audit_image` 后，
> `SmartErpAuditChainBroken` 在 t+0s `inactive` → t+11s `pending` → t+83s `firing`，
> Alertmanager `/api/v2/alerts` 出现 `severity=P0` 条目，`status.state=active`，receiver=`erp-unrouted`。

