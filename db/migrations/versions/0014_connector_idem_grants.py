"""连接器幂等表授权到 erp_app（B-IDEM-INMEMORY-ONLY）

Revision ID: 0014_connector_idem_grants
Revises: 0013_ticket_plan_step
Create Date: 2026-11-11

``erp.connector_idempotency`` 由 0002 建表，但此前没有任何代码写它：连接器幂等
只在进程内存里，重灌库不重启进程时同一 ``idem_key`` 会被误判 replay。0014 起由
``apps.connectors.idempotency_pg.PostgresIdempotencyStore`` 落库，这里补上运行期
需要的权限：SELECT / INSERT / UPDATE（begin→in_flight、complete/fail 推进状态）。

幂等记录是「一次定稿、只推进状态」的证据型数据，不允许应用侧删除或清空：
按仓库惯例撤掉 DELETE / TRUNCATE，清理只能由迁移角色按 ``expires_at`` 执行。
"""

from __future__ import annotations

from alembic import op

revision = "0014_connector_idem_grants"
down_revision = "0013_ticket_plan_step"
branch_labels = None
depends_on = None

SCHEMA = "erp"
TABLE = "connector_idempotency"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
                GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.{TABLE} TO erp_app;
                REVOKE DELETE, TRUNCATE ON {SCHEMA}.{TABLE} FROM erp_app;
            END IF;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
                GRANT DELETE ON {SCHEMA}.{TABLE} TO erp_app;
            END IF;
        END;
        $$
        """
    )
