# S8 交付说明：worker / scheduler / 健康探针（B-S8-01）

> 归属：S3（契约 C15）。本文件只写「交付了什么、S8 要改什么、怎么复验」。
> 目录边界：S3 只改 `core/runtime/**`、`tools/health.py`、`pyproject.toml`、`tests/agents/**`；
> `deploy/smart-erp/**`、`projects/smart-ERP/docker/**`、`scripts/*.sh` 未动，需 S8 按下文修改。
> S3 不自行判过，最终由 S8 复验关闭。

## 一、交付内容

| 文件 | 作用 |
|------|------|
| `core/runtime/queue.py` | `erp.job_queue` 存取：`FOR UPDATE SKIP LOCKED` 认领、完成、失败退避、死信、stale 回收、状态统计 |
| `core/runtime/worker.py` | `python -m core.runtime.worker --queues agent,io --concurrency 4`；落库心跳、长任务续租、SIGTERM 优雅退出 |
| `core/runtime/scheduler.py` | `python -m core.runtime.scheduler --lease-seconds 30 --tick-seconds 5`；`erp.scheduler_lease` 库内租约、tick 回收 stale、可选 `--audit-verify` |
| `tools/health.py` | `python -m tools.health worker\|scheduler`；8011 HTTP `/healthz`，或 `--check` 一次性输出 JSON 并以 0/1 退出 |
| `db/migrations/versions/0005_worker_runtime.py` | `erp.worker_heartbeat`、`erp.scheduler_lease`，`down_revision = 0004_audit_chain_anchor` |
| `pyproject.toml` | `packages.find.include = ["apps*","core*","tools*"]`，否则镜像内 `ModuleNotFoundError` |
| `tests/agents/test_worker_runtime.py` | 消费/重试/死信/stale 回收/租约抢锁与接管/探针 |
| `tests/agents/test_runtime_packaging.py` | 按 pyproject 规则跑 `find_packages` + 真实导入 + `python -m … --help` |
| `tests/agents/s8_evidence.py` | **一条命令复跑三.1~三.4 四个场景，打印可核对数字**；`test_s8_evidence.py` 是同逻辑的 pytest 入口 |

worker 内置两种 kind 用于冒烟：`echo`（空操作）、`sleep`（`payload.seconds` 秒）。
业务 kind 通过 `core.runtime.worker.register_handler(kind)` 注册。

## 二、S8 需要修改的地方（S3 不改这些目录）

1. **重建 app 镜像**：本 PR 合入后重打 `smart-erp/app:<sha>`，镜像内自检：
   ```
   docker run --rm smart-erp/app:<sha> python -c "import core.runtime.worker, core.runtime.scheduler, tools.health"
   ```
2. **compose 命令**（四个服务同一入口，队列名区分）：
   - `worker-agent`：`python -m core.runtime.worker --queues agent --concurrency 4`
   - `worker-io`：`python -m core.runtime.worker --queues io --concurrency 4`
   - demo `worker`：`python -m core.runtime.worker --queues agent,io --concurrency 4`
   - `scheduler`：`python -m core.runtime.scheduler --lease-seconds 30 --tick-seconds 5`
3. **healthcheck**：优先在**同一容器内** exec（hostname 一致，探针默认按 `hostname:%` 过滤心跳）：
   ```
   test: ["CMD", "python", "-m", "tools.health", "worker", "--check", "--queues", "agent"]
   ```
   scheduler 用 `python -m tools.health scheduler --check`。
   若要保留 8011 HTTP 探针，则容器内常驻 `python -m tools.health worker`，探 `GET /healthz`。
   **注意**：若把探针放进独立 sidecar 容器，hostname 不同，必须显式传 `--worker-id <worker 的 id>`，
   否则永远 503。
4. **restart 策略**：worker/scheduler 用 `restart: unless-stopped`；探针失败即重启，靠
   `worker_heartbeat` / `scheduler_lease` 的库内状态判定，不依赖进程内变量。
5. **调度副本数**：compose 默认 scheduler 1 副本即可；即使误扩到 2，库内租约保证只有一个
   tick，另一个空转等待，不会双跑。
6. **迁移**：`migrate` 容器会沿链升到 `0005_worker_runtime`；0004 的回填校验若发现断链会
   中止升级（版本停在 0003），这是预期行为，先修数据再升级。

## 三、本机实测证据（S8 可复跑）

环境：PG15 容器 `127.0.0.1:15432/smart_erp`；`ERP_DATABASE_URL` 指向该库。

**一条命令复跑全部四个场景**（先 `alembic upgrade head`）：

```powershell
$env:ERP_TEST_DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:15432/smart_erp"
python tests/agents/s8_evidence.py
```

输出四行 JSON + `SUMMARY 4/4 passed`，不符预期退出码 1；CI 侧同逻辑入口为
`tests/agents/test_s8_evidence.py`。2026-11-11 本机结果：

1. **CLI 真消费**：入队 5 条 `echo` →
   `python -m core.runtime.worker --queues agent --worker-id s8-cli --max-jobs 5`
   后 `job_queue` 状态 `succeeded=5`，`worker_heartbeat` 行 `(s8-cli, stopped, jobs_done=5, jobs_failed=0)`，
   退出码 0。
2. **入队/完成/失败/重试数字**：入队 14 条（10 echo + 3 `boom`(max_attempts=3) + 1 未知 kind）：
   - 完成 `M=10`，失败尝试 `K=10`；
   - 终态：`succeeded` 10、`dead` 4（3 条 `boom` attempts=3 + 1 条未知 kind attempts=1）；
   - 重试次数 `sum(attempts-1)=6`；没有永久 `queued` 转圈。
3. **worker 被强杀后的回收**：`sleep 60` 任务被 `killme` 认领后强杀进程 →
   心跳停在 `busy`（探针 `--stale-seconds 2` 返回 `{"ok": false}`、退出码 1）→
   租约到期后 `reclaim_stale()=1` → 新 owner `claim` 成功（`attempts=2`）→ 完成。
4. **scheduler 单点**：两个进程同时启动（`sched-A` / `sched-B`，lease 4s），
   只有 `sched-B` 日志出现「获得租约」；强杀 B 后约 3s，`sched-A` 日志出现「获得租约」并接管。
5. **迁移往返**：`alembic downgrade 0004` 后两表 `to_regclass` 均为 NULL、版本 0004；
   `upgrade head` 后两表恢复、版本 0005。
6. **8011 HTTP 探针**：`python -m tools.health worker --port 18011 --worker-id w-stats`
   → `GET /healthz` 返回 200，body 含 `alive_workers[].worker_id/state/age_seconds/jobs_done`
   与 `queue_depth`；心跳超时则返回 503，`--check` 模式退出码 1。

## 四、S8 复验清单（全部满足才可关闭 B-S8-01）

- [ ] `worker-agent` / `worker-io` / `scheduler` 三个容器 **healthy**（demo 档 `worker` 同）；
- [ ] 8011 探针 `/healthz` 200（或 `tools.health … --check` 退出码 0）；
- [ ] `SELECT state, count(*) FROM erp.job_queue GROUP BY state`：`succeeded` 随入队增长；
- [ ] `SELECT worker_id, state, jobs_done, jobs_failed, heartbeat_at FROM erp.worker_heartbeat`
      有行且 `heartbeat_at` 持续刷新；
- [ ] `SELECT owner, expires_at FROM erp.scheduler_lease` 只有一个非空 owner，且 `expires_at` 未过期；
- [ ] `docker kill` worker 后：心跳停止 → 探针转 503 → 租约过期后任务可被新 worker 认领；
- [ ] 把 scheduler 扩到 2 副本：只有一个持有租约；杀掉持有者后另一个在 `lease-seconds` 内接管；
- [ ] 镜像内 `python -c "import core.runtime.worker, core.runtime.scheduler, tools.health"` 成功；
- [ ] `python tests/agents/s8_evidence.py` 输出 `SUMMARY 4/4 passed`（在 migrate 升到 head 后跑）。

## 五、参数与默认值（决定「会不会误判死亡」）

四个数都可配，默认值同时写在代码常量里（`core/runtime/queue.py`、`worker.py`、`scheduler.py`），
文档与 CLI 保持一致：

| 参数 | 默认值 | 配置位置 | 说明 |
|------|--------|----------|------|
| 心跳间隔 | **10s** | `worker --heartbeat-seconds`（`DEFAULT_HEARTBEAT_SECONDS`） | 写 `erp.worker_heartbeat`；必须 `< 租约 TTL`，代码启动即校验 |
| 租约 TTL | **60s** | `worker --lease-seconds`（`DEFAULT_LEASE_SECONDS`） | 认领时写入 `lease_expires_at`；过期后别人才能回收 |
| stale 判定 | **300s**（5 分钟） | `tools.health … --stale-seconds`（`DEFAULT_STALE_SECONDS`） | 探针查不到近 5 分钟心跳即 503；任务 stale 由租约 TTL 判定，不用这个值 |
| 最大 attempts | **3** | `enqueue(max_attempts=…)`（`DEFAULT_MAX_ATTEMPTS`），可按任务覆盖 | 到上限转 `dead` 终态，等人工重放，不无限重试 |
| 失败退避 | **5s** | `worker --retry-backoff-seconds`（`DEFAULT_RETRY_BACKOFF_SECONDS`） | 失败回队列的等待时间 |
| 回收批次 | **100 条/次** | `reclaim_stale(limit=…)`（`DEFAULT_RECLAIM_BATCH`） | 避免单事务锁太多行 |

防误判约束：worker 启动时校验 `0 < heartbeat_seconds < lease_seconds`，否则直接拒绝启动；
scheduler 续约间隔取 `min(tick_seconds, lease_seconds/2)`，tick 配大也不会出现「续约前先过期」。
机器较慢或任务较重时，先调大 `--lease-seconds` 与 `--heartbeat-seconds`，不要只调探针的
`--stale-seconds`（那只影响容器健康判定，不改变任务回收）。

## 六、PR 描述用：新增表清单（给 S0 对照 `BASELINE_TABLES`）

`0004_audit_chain_anchor`：`erp.ticket_event_anchor`、`erp.audit_guard_state`
（另给 `erp.compensation_task` 加 `lease_owner` / `heartbeat_at` 两列与 stale 索引）。

`0005_worker_runtime`：`erp.worker_heartbeat`、`erp.scheduler_lease`。

API 流 `0003_recon_persistence`：`erp.recon_batch`、`erp.recon_line`、
`erp.recon_line_event`、`erp.recon_line_link`。合计 8 张新表；`tests/platform` 的
`BASELINE_TABLES` 由 S0 更新，S3 未改该目录。

rebase 前置：确认基线为 `4f8bc5d`（PR #31 已合，主干已前进），且分支已包含 main 的
最新提交（避免重演 API 流「分支缺 main 提交」的问题）；`0003` 进 main 后再 rebase，
PR 携带 `0004 + 0005`。

### 待提交文件清单（S3 分支，按目录分组）

```
core/__init__.py
core/runtime/__init__.py
core/runtime/queue.py
core/runtime/worker.py
core/runtime/scheduler.py
tools/__init__.py
tools/health.py
pyproject.toml
db/migrations/versions/0004_audit_chain_anchor.py
db/migrations/versions/0005_worker_runtime.py
apps/agents/models.py
apps/agents/store.py
apps/agents/audit_guard.py
apps/agents/AUDIT_GUARD.md
apps/agents/MIGRATION_NEEDS.md
tests/agents/conftest.py
tests/agents/dbutil.py
tests/agents/test_audit_guard.py
tests/agents/test_schema_parity.py
tests/agents/test_worker_runtime.py
tests/agents/test_runtime_packaging.py
tests/agents/s8_evidence.py
tests/agents/test_s8_evidence.py
docs/product/tech/S8-worker-runtime-handoff.md
docs/product/tech/ledger/S3.jsonl
```

`tests/agents/s8_evidence.py` 与 `test_s8_evidence.py` 必须入库：它们是 4/4 证据的
唯一可复现入口，不入库则 S8 复验与后续回归只能靠口头记录。

迁移链核对（2026-11-11 已验证）：`0004.down_revision = 0003_recon_persistence`、
`0005.down_revision = 0004_audit_chain_anchor`；把 `0003` 从迁移目录移除后离线
`upgrade head --sql` 直接报 `KeyError: '0003_recon_persistence'`——这是预期失败，
**不要**为了让分支单独可升级而改依赖链。

## 七、已知事项（不阻塞本卡，需对应流处理）

- `tests/platform/test_migrations.py` 的 `BASELINE_TABLES` 与 `test_migrations_live.py` 的
  期望表集合尚未包含 0003 recon 四表与 0004/0005 四张新表（合计 8 张，清单见第六节），
  按目录边界归 **S0** 更新；
  S3 未改 `tests/platform/**`。S3 侧守卫见 `tests/agents/test_schema_parity.py`（已覆盖 0004/0005，
  recon 四表作为 API 流原生 SQL 例外显式登记）。
- `worker` 的 drain 超时后任务会被取消并回队列（租约到期可被回收），不会丢任务；
  长任务建议控制单条时长，避免反复中断。
