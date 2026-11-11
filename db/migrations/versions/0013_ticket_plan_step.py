"""执行计划持久化：重启后不带 steps 也能续跑

Revision ID: 0013_ticket_plan_step
Revises: 0012_config_change_chain
Create Date: 2026-11-11

``ticket_step`` 只存「执行到哪一步」的 request_snapshot，没有「还剩哪些步骤」；
进程重启 / 租约过期后，恢复入口只能靠调用方把 steps 再给一遍。

本迁移新增 ``erp.ticket_plan_step``：执行前把整份计划（动作键、目标系统、请求体、
幂等键、verify 载荷）按 ``step_no`` 落库。恢复入口从这张表读计划、从代码里的
``ActionRegistry`` 取动作声明（等级/verify 不落库），因此不需要调用方重给步骤。

计划一旦落库不再改写：应用侧重复执行时会先比对，不一致直接拒绝；库权限层面对
``erp_app`` 只授 SELECT/INSERT 并撤掉 UPDATE/DELETE/TRUNCATE，避免计划被悄悄篡改。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_ticket_plan_step"
down_revision = "0012_config_change_chain"
branch_labels = None
depends_on = None

SCHEMA = "erp"


def upgrade() -> None:
    op.create_table(
        "ticket_plan_step",
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("action_key", sa.Text(), nullable=False),
        sa.Column("target_system", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idem_key", sa.Text(), nullable=False),
        sa.Column(
            "verify_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("step_no >= 1", name="ck_ticket_plan_step_no_positive"),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            [f"{SCHEMA}.ticket.id"],
            name="fk_ticket_plan_step_ticket",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ticket_id", "step_no", name="pk_ticket_plan_step"),
        schema=SCHEMA,
    )

    # 默认权限会给新表授 UPDATE/DELETE；计划是「一次定稿」的输入，应用侧只写不改。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
                GRANT SELECT, INSERT ON erp.ticket_plan_step TO erp_app;
                REVOKE UPDATE, DELETE, TRUNCATE ON erp.ticket_plan_step FROM erp_app;
            END IF;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.drop_table("ticket_plan_step", schema=SCHEMA)
