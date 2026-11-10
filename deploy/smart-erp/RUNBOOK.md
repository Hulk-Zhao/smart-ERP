# smart-ERP 运维手册（备份 / 恢复 / 升级）

> 上线当天按 [GO-LIVE-RUNBOOK.md](./GO-LIVE-RUNBOOK.md) 逐条执行（按时间轴排列、含预期输出与回滚动作）；
> 本文件是背景、细则与演练结论。

> 适用：共用 PG15 实例上的 `smart_erp` 库（`erp` schema）与 `erp_objects` 对象卷。
> 红线：**不备份、不恢复、不 DROP APS 的任何对象**；所有操作只在自己这一户里。
> 脚本：`scripts/backup-smart-erp.sh`、`scripts/restore-smart-erp.sh`、`scripts/upgrade-smart-erp.sh`。

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
#    api 以 --root-path=/erp/api 启动，下面的 proxy_pass 又保留 /erp/api/ 前缀，
#    所以容器内 /metrics 对外的实际路径是 https://<域名>/erp/api/metrics。
#    只写 location = /metrics 拦不住它（历史写法就是漏在这里）。
# ② /metrics —— 站点根路径上的同名路径，防御性兜底（不是本应用的转发路径，
#    同机其它站点或默认 server 若暴露 /metrics 会被它挡住）。
location = /erp/api/metrics {
    deny all;          # 或：return 404;
}
location = /metrics {
    deny all;          # 或：return 404;
}

# 业务 API 反代到广州宿主回环地址；注意 proxy_pass 结尾不要带 "/"，
# 保留 /erp/api/ 前缀（api 以 --root-path=/erp/api 启动）
location /erp/api/ {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}
```

**nginx 侧验收口径（两条都必须满足，任一 200 即视为指标外泄、发布不通过）：**

```bash
# ① App 指标的真实公网路径：必须 403/404，不得 200
curl -m 5 -o /dev/null -sS -w '%{http_code}\n' https://<域名>/erp/api/metrics
# 期望输出：403 或 404

# ② 站点根路径兜底：必须 403/404，不得 200
curl -m 5 -o /dev/null -sS -w '%{http_code}\n' https://<域名>/metrics
# 期望输出：403 或 404
```

配合 5.3 第 ③ 条（`http://<公网IP>:8010/metrics` 必须连接失败），三条合起来才算
「指标既不能绕过 nginx，也不能直连宿主」。换域名/改前缀后必须重跑这三条。

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

