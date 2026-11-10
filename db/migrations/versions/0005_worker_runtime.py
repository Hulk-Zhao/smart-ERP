"""C15 常驻进程：worker 心跳与 scheduler 单点租约

Revision ID: 0005_worker_runtime
Revises: 0004_audit_chain_anchor
Create Date: 2026-11-11

- ``erp.worker_heartbeat``：worker 每 N 秒 upsert 一行；健康探针只读这张表，
  近 5 分钟无新鲜心跳即失败（容器内进程卡死能被 compose 重启）。
- ``erp.scheduler_lease``：scheduler 单点租约，库里可见、带过期时间；
  两个副本同时抢时只有一个能拿到，持有者崩溃后过期可被接管。
  进程内锁不作为单点保证。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_worker_runtime"
down_revision = "0004_audit_chain_anchor"
branch_labels = None
depends_on = None

SCHEMA = "erp"


def upgrade() -> None:
    op.create_table(
        "worker_heartbeat",
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column(
            "queues",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "state", sa.Text(), nullable=False, server_default=sa.text("'starting'")
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "jobs_done", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "jobs_failed", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.PrimaryKeyConstraint("worker_id", name="pk_worker_heartbeat"),
        schema=SCHEMA,
    )
    op.create_index(
        "worker_heartbeat_recent_idx",
        "worker_heartbeat",
        ["heartbeat_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "scheduler_lease",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("scope", name="pk_scheduler_lease"),
        schema=SCHEMA,
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
                GRANT SELECT, INSERT, UPDATE ON erp.worker_heartbeat TO erp_app;
                GRANT SELECT, INSERT, UPDATE ON erp.scheduler_lease TO erp_app;
                REVOKE DELETE, TRUNCATE ON erp.worker_heartbeat FROM erp_app;
                REVOKE DELETE, TRUNCATE ON erp.scheduler_lease FROM erp_app;
            END IF;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.drop_table("scheduler_lease", schema=SCHEMA)
    op.drop_index(
        "worker_heartbeat_recent_idx", table_name="worker_heartbeat", schema=SCHEMA
    )
    op.drop_table("worker_heartbeat", schema=SCHEMA)
