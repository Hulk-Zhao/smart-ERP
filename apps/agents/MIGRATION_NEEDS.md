# S3 迁移说明：`erp.compensation_task` 与审计链锚点

> 归属：S3 运行时。**迁移已由 S3 自己写**（`db/migrations/versions/0004_audit_chain_anchor.py`，
> `down_revision = 0003_recon_persistence`），S0 只做链上复核，不再代写。
> 0002 由 S1/S3/S4 合并窗口落地；0004 须等 0003 合入 main 后 rebase 再开 PR。
>
> **共享 Base 边界（务必对齐）**：S3 向 `apps.platform.db.base.Base.metadata` 注册了
> **5 张** 表：`erp.compensation_task`、`erp.ticket_event_anchor`、`erp.audit_guard_state`、
> `erp.worker_heartbeat`、`erp.scheduler_lease`（定义在 `apps/agents/models.py`）。
> 迁移与 metadata 的表集合必须双向相等，守卫测试见 `tests/agents/test_schema_parity.py`
> （API 流 0003 的 recon 四表用原生 SQL、无 ORM，登记在测试的 `MIGRATION_ONLY_TABLES`）。

## 1. 为什么需要

S0 基线只有 `ticket` / `ticket_event` / `ticket_step` / `job_queue`。补偿必须
「独立队列 + `compensate_seq` 倒序」，而 `job_queue` 的 `id` 自增无法表达
「按原步骤序号倒序」。运行时代码已按本表实现（`apps/agents/compensation.py`），
测试用等价的建表语句跑通；缺迁移时生产无法启动补偿 worker。

## 2. 表结构（与 `apps/agents/models.py` 一致）

```sql
CREATE TABLE erp.compensation_task (
  id              uuid PRIMARY KEY,
  ticket_id       uuid NOT NULL REFERENCES erp.ticket(id),
  original_step   uuid NOT NULL REFERENCES erp.ticket_step(id),
  compensate_seq  int  NOT NULL,                -- 必须等于原 step_no
  action_key      text NOT NULL,
  state           text NOT NULL DEFAULT 'queued', -- queued|running|done|failed
  attempts        int  NOT NULL DEFAULT 0,
  next_retry_at   timestamptz,
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_compensation_task_seq UNIQUE (ticket_id, compensate_seq),
  CONSTRAINT ck_compensation_task_state
      CHECK (state IN ('queued', 'running', 'done', 'failed')),
  CONSTRAINT ck_compensation_task_seq_positive CHECK (compensate_seq >= 1),
  CONSTRAINT ck_compensation_task_attempts CHECK (attempts >= 0)
);

CREATE INDEX compensation_task_dequeue_idx
    ON erp.compensation_task (ticket_id, compensate_seq DESC);
```

## 3. 两条「数据结构约束」（必须一起提交）

```sql
-- (1) compensate_seq 不允许填错：必须等于 original_step.step_no
CREATE OR REPLACE FUNCTION erp.fn_compensation_seq_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    expected int;
BEGIN
    SELECT step_no INTO expected
      FROM erp.ticket_step
     WHERE id = NEW.original_step AND ticket_id = NEW.ticket_id;
    IF expected IS NULL THEN
        RAISE EXCEPTION '补偿任务必须指向同一工单的步骤: step=%', NEW.original_step;
    END IF;
    IF NEW.compensate_seq <> expected THEN
        RAISE EXCEPTION 'compensate_seq 必须等于原步骤序号: 期望 %, 实际 %',
            expected, NEW.compensate_seq;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER compensation_task_seq_guard
BEFORE INSERT OR UPDATE ON erp.compensation_task
FOR EACH ROW EXECUTE FUNCTION erp.fn_compensation_seq_guard();

-- (2) 同一工单同一时刻只允许一个补偿任务 running（§3.1.5 第 3 条）
CREATE UNIQUE INDEX compensation_task_one_running_idx
    ON erp.compensation_task (ticket_id)
    WHERE state = 'running';
```

> 迁移里用普通的 `CREATE TRIGGER` / `CREATE INDEX` 即可；`apps/agents/models.py`
> 的 `ADDITIONAL_DDL` 改用 `CREATE OR REPLACE TRIGGER` 与 `CREATE ... IF NOT EXISTS`，
> 只为让本地测试重复执行时幂等（PG15 支持），语义与上表完全一致。

## 4. 权限与 RLS

- 应用账号只需 `SELECT/INSERT/UPDATE`（补偿任务会被更新为 running/done/failed）；
  **不需要** `DELETE`——补偿记录属于审计证据；
- 与其余表一致挂租户 RLS 策略时，`ticket_id` 关联 `ticket.tenant_id` 过滤。

## 5. 验收

1. `alembic upgrade head` 在空库、0001 旧库均可执行；
2. 插入 `compensate_seq <> step_no` 必须报错（断言错误信息含 `compensate_seq`）；
3. 同工单两条 `state='running'` 必须触发唯一索引冲突；
4. 运行 `python -m pytest tests/agents/test_compensation_queue.py` 全绿。

## 6. 兼容性

- 0002 只新增 `compensation_task`，不改 0001 的四张表；
- 0004 只新增两张审计链表与补偿租约两列，对存量 `ticket_event` 做一次锚点回填
  （取每工单链尾快照，不修改任何事件）；
- 若后续把 0004 并入其他迁移文件，只需保证第 2、3、7 节的约束语义不变。

## 7. 增量（混沌演练 D3/D4 修复）——已由 `0004_audit_chain_anchor` 落地

> 来源：`docs/product/tech/chaos-report.md`。迁移文件：
> `db/migrations/versions/0004_audit_chain_anchor.py`（`down_revision = 0003_recon_persistence`）。
> 本节为迁移内容说明，供 S0 链上复核；`apps/agents/models.py` 已同步注册
> `TicketEventAnchor` / `AuditGuardState` 两个 ORM 模型。

### 7.1 补偿任务租约（D3）

```sql
ALTER TABLE erp.compensation_task
    ADD COLUMN lease_owner   text,
    ADD COLUMN heartbeat_at  timestamptz;

-- stale running 回收扫描（state='running' AND heartbeat_at < now()-ttl）
CREATE INDEX compensation_task_stale_idx
    ON erp.compensation_task (state, heartbeat_at)
    WHERE state = 'running';
```

语义：
- `claim_next` 在 `FOR UPDATE SKIP LOCKED` 下认领，同时写 `lease_owner` / `heartbeat_at=now()` /
  `attempts=attempts+1`；执行期间按间隔心跳续期；
- `heartbeat_at` 超过 TTL 的 `running` 视为 owner 已死，由
  `CompensationQueue.reclaim_stale*` 重置为 `queued` 重新入队（不改 `compensate_seq`，倒序不变）；
- `attempts` 超上限 → `failed` + P0 告警 + 工单转需人工处置；
- **单跑语义由状态机 + `FOR UPDATE SKIP LOCKED` 保证**，第 3 节的
  `compensation_task_one_running_idx` 只做兜底，不作为并发控制手段；
- 应用账号需要 `UPDATE` 这两列；无需 `DELETE`。

### 7.2 审计链当日锚点（D4 热路径 O(1) 校验）

```sql
CREATE TABLE erp.ticket_event_anchor (
    ticket_id  uuid PRIMARY KEY REFERENCES erp.ticket(id) ON DELETE CASCADE,
    seq        integer NOT NULL,
    hash       text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

语义：与每次 `ticket_event` 追加**同事务** upsert（`ON CONFLICT (ticket_id) DO UPDATE`）。
每次 `execute` 前只读一行锚点 + 链尾事件，比对 `seq/hash` 与 `prev_hash`；
不一致即拒绝执行并报审计链异常（fail-closed）。中间事件篡改由离线全链巡检兜底。

升级时对存量事件回填锚点：**先逐工单全链重放校验**，任一处哈希不匹配或
`prev_hash` 不衔接就中止迁移并打印 `工单/seq` 断点，版本停在 0003、不建锚点表；
全部通过后才用 `SELECT DISTINCT ON (ticket_id) ... ORDER BY seq DESC`
取每工单链尾快照回填（`ON CONFLICT DO NOTHING`）。不回填会让存量工单因「缺锚点」
被热路径全部判为断链；回填前做校验则避免把一段已损坏的链尾固化成锚点、把问题藏起来。

### 7.3 全局写入开关（D4 降级只读）

```sql
CREATE TABLE erp.audit_guard_state (
    scope       text PRIMARY KEY,
    mode        text NOT NULL,           -- writable | readonly
    reason      text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
```

语义：全链巡检（`daily_verify()` / `hourly_verify()` / `run_periodic()`）发现任一链断裂
→ **先**写 `scope='global', mode='readonly'`，**再**发 P0（冻结先于告警，时延≈0）；
此后所有写类动作（level > W0）拒绝执行，W0 只读不受影响。
巡检全绿只刷新锚点，**不自动恢复** `writable`——须人工确认后显式调用
`set_global_mode(WRITABLE, ...)`。

### 7.4 验收（增量）

1. 空库 `alembic upgrade head` 可执行（0001→0002→0003→0004），
   且 `compensation_task` 具备两新列与 `compensation_task_stale_idx`；
2. `erp.ticket_event_anchor` / `erp.audit_guard_state` 存在，删除 ticket 时 anchor 级联删除；
3. `python -m pytest tests/agents/test_compensation_recovery.py tests/agents/test_audit_guard.py -q` 全绿；
4. `python -m pytest tests/agents/test_schema_parity.py -q`：迁移 head 表集合与 ORM metadata
   双向差集为空（含本文件新增两表）。

## 8. 增量（C15 常驻进程）——已由 `0005_worker_runtime` 落地

> `db/migrations/versions/0005_worker_runtime.py`（`down_revision = 0004_audit_chain_anchor`）。
> 解决 B-S8-01：worker / scheduler 起不来不只是缺文件，心跳与单点租约必须有落库载体。

### 8.1 `erp.worker_heartbeat`（worker 落库心跳）

| 列 | 类型 | 说明 |
|----|------|------|
| `worker_id` | text PK | 默认 `hostname:pid`，同一容器内健康探针按 hostname 前缀过滤 |
| `queues` | text[] NOT NULL default `'{}'` | 本进程消费的队列 |
| `state` | text NOT NULL default `'starting'` | `starting` / `idle` / `busy` / `stopped` |
| `started_at` / `heartbeat_at` | timestamptz NOT NULL default `now()` | 存活判定只看 `heartbeat_at` |
| `jobs_done` / `jobs_failed` | bigint NOT NULL default 0 | 复验时核对消费数字；**按进程累计，重启归零**，全局以 `job_queue` 终态为准 |

索引 `worker_heartbeat_recent_idx(heartbeat_at)`。**心跳必须是数据库行，不能是进程内变量**：
worker 被 kill 后行停止更新，`tools.health worker` 在 `--stale-seconds`（默认 300s）内
查不到新鲜心跳即返回 503，compose 可据此重启容器。

### 8.2 `erp.scheduler_lease`（scheduler 单点租约）

| 列 | 类型 | 说明 |
|----|------|------|
| `scope` | text PK | 默认 `scheduler`，为将来的分片调度留位 |
| `owner` | text NULL | 当前持有者；`NULL` 表示已释放 |
| `expires_at` | timestamptz NULL | 过期即可被其他副本接管 |
| `heartbeat_at` / `updated_at` | timestamptz | 心跳与最近变更时间 |

抢锁/续约同一条 `INSERT ... ON CONFLICT DO UPDATE ... WHERE owner 是自己 OR expires_at < now()`
（`core/runtime/scheduler.py`）：两个副本同时启动只有一个能 `RETURNING`，不依赖进程内锁；
持有者崩溃后租约过期，另一个副本下一 tick 接管。

### 8.3 权限

存在 `erp_app` 角色时授予两表 `SELECT/INSERT/UPDATE`，`REVOKE DELETE/TRUNCATE`
（与 0004 一致：运行时可写，不可删表数据）。

### 8.4 验收（增量）

1. 空库 `alembic upgrade head` 可执行（0001→…→0005），两表存在、索引存在；
2. `downgrade 0005 → 0004` 后再 upgrade 成功；
3. `tests/agents/test_schema_parity.py` 双向差集为空（含两新表）；
4. S8 复验：容器 healthy、8011 探针 200、`job_queue` 有消费、`worker_heartbeat` 有行。

## 9. `ticket_step` 的定位：可变执行投影，不是证据

> 裁定依据：`01-技术架构.md:581`（步骤级真源、状态字段受控 UPDATE）与 `:650`
> （检查点 = 每步一行终态）。落地见 `0006_audit_image_prev_uniq`。

**不加 append-only 触发器。** `AppStore.mark_step` 必须更新 `state` /
`response_snapshot` / `error_code` / `latency_ms` / `before_ref` / `after_ref`；
加触发器会直接打断断点续跑、重试与补偿（补偿依据 `original_step.step_no`）。
所以 `ticket_step` 是**可变投影**：允许改，但必须留下事件。

替代约束（两者缺一不可）：

1. **授权收窄**：`erp_app` 只有 `SELECT` / `INSERT` 与上述六列的 `UPDATE`，
   整表 `UPDATE`、`DELETE`、`TRUNCATE` 全部收回（`0006` 迁移执行）；
2. **变更必须伴随事件**：每次步骤变更在同一事务里追加 `STEP_EVENT`
   （`tests/agents/test_step_events.py` 断言事件序列与投影终态一致）。

**任何以步骤行为依据的结论，必须回到 `ticket_event` 重放**——投影被绕过改写时
（例如有更高权限的连接直改库），事件链才是可校验的那一份。
