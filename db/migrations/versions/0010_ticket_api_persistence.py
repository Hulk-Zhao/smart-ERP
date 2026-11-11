"""API 工单持久化：补齐 ``ticket`` / ``ticket_step`` 的接口所需列。

API 侧工单此前只存在于进程内 ``TicketRepository``，读路径要的字段在 C3 基线表里
没有，直接复用会让「列表 scope 过滤」「详情步骤」「审批通过后按锁定的写计划执行」
全部落空：

- ``ticket`` 缺 ``org_id`` / ``period`` / ``systems`` / ``write_level`` / ``amount`` /
  ``updated_at`` / ``pending_write``。``diff_amount`` 是「差异金额」且可空，不能当
  工单金额；``systems_count`` 只有数量，做不了 scope 子集判定（``systems <@ scope``）。
- ``ticket_step`` 缺 ``object_type`` / ``object_id``（详情要输出）、``audit_image_id``。

``org_id`` / ``period`` / ``systems`` 保持可空：执行器建的历史工单没有这些信息，
「未知」不能用空串或空数组冒充——空数组会让 scope 子集判定恒真，等于把工单发给
所有 scope。

只加列与一个索引，不建新表、不碰 ``compensation_task``（已关闭的同步回滚路径）。
``audit_image_id`` 不加外键：API 的审计影像可能只在进程内账本里，加外键会让
「先写步骤、后落影像」的两段提交在第一段就失败。

链：``0009_compensation_updated_at`` → 本迁移。

Revision ID: 0010_ticket_api_persistence
Revises: 0009_compensation_updated_at
"""

from __future__ import annotations

from alembic import op

revision = "0010_ticket_api_persistence"
down_revision = "0009_compensation_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE erp.ticket
            ADD COLUMN IF NOT EXISTS org_id text,
            ADD COLUMN IF NOT EXISTS period text,
            ADD COLUMN IF NOT EXISTS systems text[],
            ADD COLUMN IF NOT EXISTS write_level smallint,
            ADD COLUMN IF NOT EXISTS amount numeric(18, 4),
            ADD COLUMN IF NOT EXISTS pending_write jsonb,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()
        """
    )
    # W3 允许出现在工单投影（审批必拒），但永远不落 ticket_step（见 0001 的 0..2 约束）。
    op.execute(
        """
        ALTER TABLE erp.ticket
            ADD CONSTRAINT ck_ticket_write_level
            CHECK (write_level IS NULL OR write_level BETWEEN 0 AND 3)
        """
    )
    op.execute(
        """
        ALTER TABLE erp.ticket
            ADD CONSTRAINT ck_ticket_amount_non_negative
            CHECK (amount IS NULL OR amount >= 0)
        """
    )
    # 列表默认排序是 (created_at DESC, id DESC) 的键集分页；现有
    # ticket_tenant_state_created_idx 第二列是 state，不带状态过滤时供不了序。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ticket_tenant_created_id_idx
            ON erp.ticket (tenant_id, created_at DESC, id DESC)
        """
    )

    # executed_at：详情要输出步骤执行时间，C3 的 ticket_step 只有 ``attempt``/
    # ``latency_ms``，没有时间戳；不补这列则换 DB 后详情里该字段恒为 null。
    op.execute(
        """
        ALTER TABLE erp.ticket_step
            ADD COLUMN IF NOT EXISTS object_type text,
            ADD COLUMN IF NOT EXISTS object_id text,
            ADD COLUMN IF NOT EXISTS audit_image_id uuid,
            ADD COLUMN IF NOT EXISTS executed_at timestamptz
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE erp.ticket_step DROP COLUMN IF EXISTS executed_at")
    op.execute("ALTER TABLE erp.ticket_step DROP COLUMN IF EXISTS audit_image_id")
    op.execute("ALTER TABLE erp.ticket_step DROP COLUMN IF EXISTS object_id")
    op.execute("ALTER TABLE erp.ticket_step DROP COLUMN IF EXISTS object_type")

    op.execute("DROP INDEX IF EXISTS erp.ticket_tenant_created_id_idx")
    op.execute(
        "ALTER TABLE erp.ticket DROP CONSTRAINT IF EXISTS ck_ticket_amount_non_negative"
    )
    op.execute("ALTER TABLE erp.ticket DROP CONSTRAINT IF EXISTS ck_ticket_write_level")
    op.execute(
        """
        ALTER TABLE erp.ticket
            DROP COLUMN IF EXISTS updated_at,
            DROP COLUMN IF EXISTS pending_write,
            DROP COLUMN IF EXISTS amount,
            DROP COLUMN IF EXISTS write_level,
            DROP COLUMN IF EXISTS systems,
            DROP COLUMN IF EXISTS period,
            DROP COLUMN IF EXISTS org_id
        """
    )
