# S3 → S0 迁移需求：`erp.compensation_task`

> 归属：S3 运行时。S3 不改 `db/migrations/`，本文件交集成会话转 S0 合并。
> 目标版本：新增 `0002_s3_compensation_task.py`（在 `0001_s0_baseline` 之后）。
>
> **共享 Base 边界（务必对齐）**：S3 只向 `apps.platform.db.base.Base.metadata`
> 注册了 **1 张** 表 `erp.compensation_task`（定义在 `apps/agents/models.py`），
> 没有新增/修改其他表。凡是 import `apps.agents.runtime` / `apps.agents.compensation`
> 的进程，metadata 里都会出现这张表；S0 的迁移一致性测试应按「表集合」断言，
> 或显式把本表加入期望集合。迁移 0002 落地后，本文件的第 2、3 节即为验收口径。

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

- 不修改 0001 的四张表，不新增列，无数据回填；
- 若 S0 选择把该表并入其他迁移文件，只需保证第 2、3 节的约束语义不变，
  `apps/agents/models.py` 的 SQLAlchemy 定义可同步删除 `ADDITIONAL_DDL` 兜底。
