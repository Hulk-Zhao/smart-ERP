"""补偿队列 ``updated_at``：overdue 观测口径需要「最后一次状态变化」的时间。

没有这一列时，``next_retry_at IS NULL`` 的 queued 行只能退回 ``created_at``，
一条早先创建、刚刚失败回队的任务会被立刻误判为 overdue（假 P1）。

链：``0008_metering_price_book``（#79）→ 本迁移。

Revision ID: 0009_compensation_updated_at
Revises: 0008_metering_price_book
"""

from __future__ import annotations

from alembic import op

revision = "0009_compensation_updated_at"
down_revision = "0008_metering_price_book"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE erp.compensation_task
            ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()
        """
    )
    # 观测每轮扫一遍 queued；只索引在队的行，避免把历史 done/failed 也算进去。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS compensation_task_queued_updated_idx
            ON erp.compensation_task (state, updated_at)
         WHERE state = 'queued'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS erp.compensation_task_queued_updated_idx")
    op.execute(
        "ALTER TABLE erp.compensation_task DROP COLUMN IF EXISTS updated_at"
    )
