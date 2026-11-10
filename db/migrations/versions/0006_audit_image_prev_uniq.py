"""审计影像同父唯一、租户一致外键、租约与步骤写入收窄

Revision ID: 0006_audit_image_prev_uniq
Revises: 0005_worker_runtime
Create Date: 2026-11-11

- ``uq_audit_image_parent (tenant_id, prev_hash)``：同一租户下每个链尾只能有一个
  后继。此前 ``AuditStore.append`` 读链尾不加锁、``chain_seq`` 由 Identity 生成，
  同租户并发追加会算出同一个 ``prev_hash`` 却都通过 ``fn_audit_image_chain_guard``
  （快照看不到对方未提交的行），产生分叉；唯一约束把这种情况变成写入失败
  （fail-closed），是库层保证，不依赖调用方记得加锁。首条记录的
  ``prev_hash = ''`` 每租户只有一个，不受影响。
  **若生产库已存在同父分叉数据，本迁移会主动报错并拒绝升级**——先按
  ``ticket_event`` 重放修复数据，再重跑迁移。
- ``ticket(id, tenant_id)`` 唯一 + ``audit_image`` / ``ticket_event`` 复合外键：
  防止「写错租户却挂到正确工单」把影像/事件挂进另一个租户的链。同样先检查存量，
  有跨租户挂载就带计数报错退出。
- ``erp.credential_lease`` 收回整表 UPDATE/DELETE，只留 SELECT/INSERT：当前代码
  没有任何 UPDATE 路径（吊销列尚未建模），整表 UPDATE 等于让应用角色能改
  scope/narrowed_fields。将来若做吊销，先加 ``revoked_at`` / ``revoked_reason``
  列，再只授这两列的 UPDATE。
- ``erp.ticket_step`` **不加** append-only 触发器，按「可变执行投影、不是证据」
  处理：``AppStore.mark_step`` 需要更新 ``state`` / ``response_snapshot`` /
  ``error_code`` / ``latency_ms`` / ``before_ref`` / ``after_ref``，加触发器会
  直接打断断点续跑与补偿。反过来把 UPDATE 收窄到这六列（其余列应用角色不可改），
  并由 ``tests/agents/test_step_events.py`` 断言每次变更都伴随一条 ``STEP_EVENT``。
  **证据只认 ``ticket_event`` 重放**，任何以步骤行为依据的结论必须回到事件链。
"""

from __future__ import annotations

from alembic import op

revision = "0006_audit_image_prev_uniq"
down_revision = "0005_worker_runtime"
branch_labels = None
depends_on = None

SCHEMA = "erp"

# mark_step 实际会写的列；收窄授权后其它列应用角色不可 UPDATE
STEP_MUTABLE_COLUMNS = (
    "state",
    "response_snapshot",
    "error_code",
    "latency_ms",
    "before_ref",
    "after_ref",
)


def upgrade() -> None:
    # 1) 审计影像：同租户同父只允许一条。先显式检查，报错信息可直接定位数据。
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
              FROM {SCHEMA}.audit_image
             GROUP BY tenant_id, prev_hash
            HAVING count(*) > 1
             LIMIT 1;

            IF dup_count IS NOT NULL THEN
                RAISE EXCEPTION
                    'audit_image 已存在同父分叉（tenant_id=%, prev_hash=%, % 条），'
                    '拒绝建立唯一约束；请先按 ticket_event 重放修复数据',
                    dup_tenant, dup_prev, dup_count;
            END IF;
        END $$;
        """
    )
    op.create_index(
        "uq_audit_image_parent",
        "audit_image",
        ["tenant_id", "prev_hash"],
        unique=True,
        schema=SCHEMA,
    )

    # 2) 租户一致性：复合外键要求 ticket 上有 (id, tenant_id) 唯一键。
    #    同样先显式检查存量数据，避免把「外键约束冲突」这种无上下文的报错抛给运维。
    op.execute(
        f"""
        DO $$
        DECLARE
            bad_image bigint;
            bad_event bigint;
        BEGIN
            SELECT count(*) INTO bad_image
              FROM {SCHEMA}.audit_image a
              JOIN {SCHEMA}.ticket t ON t.id = a.ticket_id
             WHERE t.tenant_id <> a.tenant_id;

            SELECT count(*) INTO bad_event
              FROM {SCHEMA}.ticket_event e
              JOIN {SCHEMA}.ticket t ON t.id = e.ticket_id
             WHERE t.tenant_id <> e.tenant_id;

            IF bad_image > 0 OR bad_event > 0 THEN
                RAISE EXCEPTION
                    '存在租户与工单不一致的行（audit_image=% 条，ticket_event=% 条），'
                    '拒绝建立复合外键；请先定位并修复这些跨租户挂载',
                    bad_image, bad_event;
            END IF;
        END $$;
        """
    )
    op.create_index(
        "uq_ticket_id_tenant",
        "ticket",
        ["id", "tenant_id"],
        unique=True,
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_audit_image_ticket_tenant",
        "audit_image",
        "ticket",
        ["ticket_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_ticket_event_ticket_tenant",
        "ticket_event",
        "ticket",
        ["ticket_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )

    # 3) 租约表与步骤表：授权收窄。角色不存在时跳过（本地/CI 可能直接用库 owner 连接）。
    columns = ", ".join(STEP_MUTABLE_COLUMNS)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
                REVOKE UPDATE, DELETE ON {SCHEMA}.credential_lease FROM erp_app;
                GRANT SELECT, INSERT ON {SCHEMA}.credential_lease TO erp_app;

                REVOKE UPDATE, DELETE, TRUNCATE ON {SCHEMA}.ticket_step FROM erp_app;
                GRANT SELECT, INSERT ON {SCHEMA}.ticket_step TO erp_app;
                GRANT UPDATE ({columns}) ON {SCHEMA}.ticket_step TO erp_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    columns = ", ".join(STEP_MUTABLE_COLUMNS)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
                GRANT UPDATE, DELETE ON {SCHEMA}.credential_lease TO erp_app;
                REVOKE UPDATE ({columns}) ON {SCHEMA}.ticket_step FROM erp_app;
                GRANT UPDATE, DELETE ON {SCHEMA}.ticket_step TO erp_app;
            END IF;
        END $$;
        """
    )
    op.drop_constraint(
        "fk_ticket_event_ticket_tenant",
        "ticket_event",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_audit_image_ticket_tenant", "audit_image", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_index("uq_ticket_id_tenant", table_name="ticket", schema=SCHEMA)
    op.drop_index("uq_audit_image_parent", table_name="audit_image", schema=SCHEMA)
