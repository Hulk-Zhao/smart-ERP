"""config_change 哈希链：链序号 + 同父唯一 + SHA-256 长度约束

Revision ID: 0012_config_change_chain
Revises: 0011_job_lease_gen
Create Date: 2026-11-11

C6 的 ``ConfigChangeService`` 此前只有内存 recorder，审计载荷从未真正落库。
本迁移把 ``config_change`` 补成可校验的哈希链，口径与 ``audit_image`` 一致：

- ``chain_seq``：租户内追加序号（Identity）。``created_at`` 在同一事务内可能
  相同，不能作为验链排序依据，``verify`` 一律按 ``chain_seq`` 读；
- ``uq_config_change_parent (tenant_id, prev_hash)``：同租户同父只允许一个后继。
  并发追加退化为写入失败（fail-closed），而不是静默分叉；
- ``hash`` / ``prev_hash`` 长度约束：拒绝把截断值或非 SHA-256 内容写进链。

存量行（本迁移前没有生产写入方，理论上为空）由 Identity 自动回填；若库里已
存在同父分叉，迁移主动报错拒绝升级——先查清来源再重跑，避免把坏链固化。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_config_change_chain"
down_revision = "0011_job_lease_gen"
branch_labels = None
depends_on = None

SCHEMA = "erp"


def upgrade() -> None:
    # 1) 先显式检查同父分叉：报错信息要能直接定位租户与 prev_hash。
    op.execute(
        f"""
        DO $$
        DECLARE
            dup_tenant uuid;
            dup_prev text;
            dup_count integer;
        BEGIN
            SELECT tenant_id, prev_hash, count(*)
              INTO dup_tenant, dup_prev, dup_count
              FROM {SCHEMA}.config_change
             GROUP BY tenant_id, prev_hash
            HAVING count(*) > 1
             LIMIT 1;

            IF dup_count IS NOT NULL THEN
                RAISE EXCEPTION
                    'config_change 已存在同父分叉（tenant_id=%, prev_hash=%, % 条），'
                    '拒绝建立唯一约束；请先查清来源并修复数据',
                    dup_tenant, dup_prev, dup_count;
            END IF;
        END $$;
        """
    )

    # 2) 链序号：Identity 会给存量行回填值（本迁移前无写入方，实际为空表）。
    op.add_column(
        "config_change",
        sa.Column("chain_seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_config_change_chain_seq",
        "config_change",
        ["tenant_id", "chain_seq"],
        unique=True,
        schema=SCHEMA,
    )
    op.create_index(
        "uq_config_change_parent",
        "config_change",
        ["tenant_id", "prev_hash"],
        unique=True,
        schema=SCHEMA,
    )

    # 3) 哈希长度约束：链上只接受完整 SHA-256 十六进制串。
    op.create_check_constraint(
        "ck_config_change_hash_sha256",
        "config_change",
        "length(hash) = 64",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_config_change_prev_hash_sha256",
        "config_change",
        "length(prev_hash) = 0 OR length(prev_hash) = 64",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_config_change_prev_hash_sha256",
        "config_change",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_config_change_hash_sha256", "config_change", schema=SCHEMA, type_="check"
    )
    op.drop_index("uq_config_change_parent", table_name="config_change", schema=SCHEMA)
    op.drop_index(
        "uq_config_change_chain_seq", table_name="config_change", schema=SCHEMA
    )
    op.drop_column("config_change", "chain_seq", schema=SCHEMA)
