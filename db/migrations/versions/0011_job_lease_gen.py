"""任务租约代次围栏 + ``run_at`` 允许 NULL（立即领取）。

两件事必须一起做，否则「立即可领取」会让代次问题更容易撞上：

1. ``run_at`` 可空：NULL = 立即可领取。原来 NOT NULL + ``now()`` 时，「入队即可领」
   只能写成 ``now()``，而 ``now()`` 是事务开始时间——同一事务先插入再认领会看到
   ``run_at > now()`` 而拿不到；跨进程时钟偏差同理。
2. ``lease_gen`` 代次围栏：每次 claim 时 +1（从 0 开始，存量行回填 0）。
   ``complete`` / ``fail`` / ``heartbeat`` 必须带回代次，和库里不一致即显式失败。
   只靠 ``lease_owner`` 挡不住「同一个 worker_id 被复用」或「回收后又被同一 owner
   领回」——旧执行会把自己的结果写成新执行的，且看不出来。

索引：认领条件写成 ``run_at IS NULL OR run_at <= now()``，走既有的部分索引
``job_queue_pick_idx``（``(queue, priority DESC, run_at) WHERE state='queued'``）：
``IS NULL`` 和 ``<= now()`` 都是它的可索引条件，规划器把两个分支合并成一次
Bitmap Index Scan（实测见 PR 说明）。代价是 ``ORDER BY ... run_at NULLS FIRST``
用不上索引顺序，候选集需要一次 Sort——但只要谓词里带 OR，这一点就躲不掉
（NULL 在 btree 里排在最后，``IS NULL`` + ``<= now()`` 不是连续区间）。
没有为 NULL 分支另建部分索引：实测 ``(queue, priority DESC, id) WHERE
state='queued' AND run_at IS NULL`` 不会被选中（两个分支都会被 pick_idx 吸收），
加了只会增加写放大。

回填策略：升级不回填——旧代码从不写 NULL，存量行 ``run_at`` 全部非空。
降级必须先把 NULL 回填成 ``now()`` 再恢复 NOT NULL：语义从「立即领取」变成
「迁移时刻已可领取」，既不把任务推到未来，也不会让它永远不可领取。
不写「数据应无 NULL」这种假设。

链：``0010_ticket_api_persistence`` → 本迁移。

Revision ID: 0011_job_lease_gen
Revises: 0010_ticket_api_persistence
"""

from __future__ import annotations

from alembic import op

revision = "0011_job_lease_gen"
down_revision = "0010_ticket_api_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 代次从 0 起；ADD COLUMN ... DEFAULT 0 会把存量行一并回填成 0。
    op.execute(
        """
        ALTER TABLE erp.job_queue
            ADD COLUMN IF NOT EXISTS lease_gen integer NOT NULL DEFAULT 0
        """
    )
    op.execute("ALTER TABLE erp.job_queue ALTER COLUMN run_at DROP NOT NULL")


def downgrade() -> None:
    # 恢复 NOT NULL 之前必须回填：NULL 的语义是「立即」，now() 保留了「迁移时刻
    # 已可领取」，不会把任务推到未来，也不会让它永远不可领取。
    op.execute("UPDATE erp.job_queue SET run_at = now() WHERE run_at IS NULL")
    op.execute("ALTER TABLE erp.job_queue ALTER COLUMN run_at SET NOT NULL")
    op.execute("ALTER TABLE erp.job_queue DROP COLUMN IF EXISTS lease_gen")
