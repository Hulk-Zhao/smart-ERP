"""S1/S3/S4 合并窗口：连接器运行时状态 + 补偿队列 + 审计链 + IAM 三表

Revision ID: 0002_s1s3s4_merge
Revises: 0001_s0_baseline
Create Date: 2026-11-10

来源（不自行发明字段）：
- ``apps/connectors/MIGRATION_NEEDS.md``：``connector_idempotency`` /
  ``connector_rate_bucket`` / ``connector_breaker_state``（S1 只有文档，未提供 ORM 模型）；
- ``apps/agents/models.py``：``erp.compensation_task`` + ``ADDITIONAL_DDL``（补偿序号一致性、
  单工单单 running 的部分唯一索引）；
- ``apps/audit/models.py`` + ``apps/audit/MIGRATION_NEEDS.md``：``audit_image`` /
  ``audit_anchor`` / ``approval_record`` / ``config_change`` / ``credential_lease``；
- ``apps/security/models.py``：IAM 三表的 CHECK / UNIQUE / 索引。

沿用 0001 规范：``erp`` schema、``erp.alembic_version_erp``、离线 ``--sql`` 可渲染、
upgrade/downgrade 对称。PG15 语法，不使用 PG16 特性。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from apps.agents.models import ADDITIONAL_DDL as COMPENSATION_DDL
from apps.agents.models import COMPENSATION_STATES

revision = "0002_s1s3s4_merge"
down_revision = "0001_s0_baseline"
branch_labels = None
depends_on = None

SCHEMA = "erp"


def _state_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    joined = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({joined})", name=name)


def upgrade() -> None:
    # ------------------------------------------------ S1 连接器运行时状态三表
    # 幂等键是写入真源，不可丢；限流桶与熔断状态可重建（MIGRATION_NEEDS.md §1–3）。
    op.create_table(
        "connector_idempotency",
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("action_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # 与 apps/connectors/db/models.py 的约束名一致（命名约定拼成
        # ck_connector_idempotency_status）
        _state_check("status", ("in_flight", "succeeded", "failed"), "status"),
        sa.PrimaryKeyConstraint("idempotency_key", name="pk_connector_idempotency"),
        schema=SCHEMA,
    )
    op.create_index(
        "connector_idempotency_connector_action_created_idx",
        "connector_idempotency",
        ["connector", "action_key", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "connector_rate_bucket",
        sa.Column("bucket_key", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Numeric(12, 4), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quota_day", sa.Date(), nullable=False),
        sa.Column("quota_used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("quota_limit", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("bucket_key", name="pk_connector_rate_bucket"),
        schema=SCHEMA,
    )

    op.create_table(
        "connector_breaker_state",
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        # 同 models.py：命名约定拼成 ck_connector_breaker_state_state
        _state_check("state", ("closed", "open", "half_open"), "state"),
        sa.PrimaryKeyConstraint("connector", name="pk_connector_breaker_state"),
        schema=SCHEMA,
    )

    # ------------------------------------------------------- compensation_task
    op.create_table(
        "compensation_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_step", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("compensate_seq", sa.Integer(), nullable=False),
        sa.Column("action_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        _state_check("state", COMPENSATION_STATES, "ck_compensation_task_state"),
        sa.CheckConstraint("compensate_seq >= 1", name="ck_compensation_task_seq_positive"),
        sa.CheckConstraint("attempts >= 0", name="ck_compensation_task_attempts"),
        sa.ForeignKeyConstraint(
            ["ticket_id"], [f"{SCHEMA}.ticket.id"], name="fk_compensation_task_ticket_id_ticket"
        ),
        sa.ForeignKeyConstraint(
            ["original_step"],
            [f"{SCHEMA}.ticket_step.id"],
            name="fk_compensation_task_original_step_ticket_step",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_compensation_task"),
        sa.UniqueConstraint("ticket_id", "compensate_seq", name="uq_compensation_task_seq"),
        schema=SCHEMA,
    )
    op.create_index(
        "compensation_task_dequeue_idx",
        "compensation_task",
        ["ticket_id", sa.text("compensate_seq DESC")],
        schema=SCHEMA,
    )

    # ------------------------------------------------------------- audit_image
    op.create_table(
        "audit_image",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chain_seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=False),
        sa.Column("before_ref", sa.Text(), nullable=True),
        sa.Column("after_ref", sa.Text(), nullable=True),
        sa.Column(
            "field_diff",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("diff_missing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("length(hash) = 64", name="ck_audit_image_hash_sha256"),
        sa.CheckConstraint(
            "length(prev_hash) = 0 OR length(prev_hash) = 64",
            name="ck_audit_image_prev_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], [f"{SCHEMA}.ticket.id"], name="fk_audit_image_ticket_id_ticket"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_image"),
        sa.UniqueConstraint("tenant_id", "chain_seq", name="uq_audit_image_chain_seq"),
        schema=SCHEMA,
    )
    op.create_index(
        "audit_image_tenant_ticket_created_idx",
        "audit_image",
        ["tenant_id", "ticket_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "audit_image_tenant_step_idx", "audit_image", ["tenant_id", "step_id"], schema=SCHEMA
    )
    op.create_index(
        "audit_image_tenant_created_idx", "audit_image", ["tenant_id", "created_at"], schema=SCHEMA
    )

    # ------------------------------------------------------------ audit_anchor
    op.create_table(
        "audit_anchor",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("last_hash", sa.Text(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("record_count >= 0", name="ck_audit_anchor_count_non_negative"),
        sa.PrimaryKeyConstraint("tenant_id", "anchor_date", name="pk_audit_anchor"),
        schema=SCHEMA,
    )
    op.create_index(
        "audit_anchor_tenant_date_idx", "audit_anchor", ["tenant_id", "anchor_date"], schema=SCHEMA
    )

    # --------------------------------------------------------- approval_record
    op.create_table(
        "approval_record",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("approver_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("initiator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "approver_roles",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("exception_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("approver_id <> initiator_id", name="ck_approval_record_sod_self"),
        sa.CheckConstraint(
            "role IN ('reviewer', 'approver', 'cfo')", name="ck_approval_record_role"
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')", name="ck_approval_record_decision"
        ),
        sa.CheckConstraint("length(btrim(comment)) > 0", name="ck_approval_record_comment"),
        sa.CheckConstraint(
            "NOT ('platform_admin' = ANY (approver_roles))",
            name="ck_approval_record_no_platform_admin",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], [f"{SCHEMA}.ticket.id"], name="fk_approval_record_ticket_id_ticket"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval_record"),
        sa.UniqueConstraint(
            "ticket_id", "approver_id", name="uq_approval_record_one_per_person"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "approval_record_ticket_idx",
        "approval_record",
        ["tenant_id", "ticket_id"],
        schema=SCHEMA,
    )

    # ----------------------------------------------------------- config_change
    op.create_table(
        "config_change",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approver_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("emergency", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "approver_id IS NULL OR approver_id <> actor_id", name="ck_config_change_dual"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_config_change"),
        schema=SCHEMA,
    )
    op.create_index(
        "config_change_tenant_created_idx",
        "config_change",
        ["tenant_id", "created_at"],
        schema=SCHEMA,
    )

    # -------------------------------------------------------- credential_lease
    op.create_table(
        "credential_lease",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "narrowed_fields",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "requires_dual_review", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("credential_ref", sa.Text(), nullable=False),
        sa.Column("degradation_reason", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('delegated', 'service_account')", name="ck_credential_lease_mode"
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_credential_lease_ttl"),
        sa.CheckConstraint(
            "mode = 'delegated' "
            "OR (requires_dual_review AND cardinality(narrowed_fields) > 0)",
            name="ck_credential_lease_b_mode",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credential_lease"),
        schema=SCHEMA,
    )
    op.create_index(
        "credential_lease_ticket_idx",
        "credential_lease",
        ["tenant_id", "ticket_id"],
        schema=SCHEMA,
    )

    # ------------------------------------------------ S3 补偿队列结构约束
    for statement in COMPENSATION_DDL:
        op.execute(statement)

    # ------------------------------------------------ S4 只追加 / 哈希链护栏
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.fn_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION '% 只允许追加，拒绝 %', TG_TABLE_NAME, TG_OP;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    for table in ("audit_image", "audit_anchor", "approval_record", "config_change"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.{table}
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.fn_append_only();
            """
        )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.fn_audit_image_chain_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            tail text;
        BEGIN
            SELECT hash INTO tail
              FROM {SCHEMA}.audit_image
             WHERE tenant_id = NEW.tenant_id
             ORDER BY chain_seq DESC
             LIMIT 1;
            IF NEW.prev_hash IS DISTINCT FROM COALESCE(tail, '') THEN
                RAISE EXCEPTION 'audit_image 链不连续: prev_hash=% 链尾=%',
                    NEW.prev_hash, COALESCE(tail, '');
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER audit_image_chain_guard
        BEFORE INSERT ON {SCHEMA}.audit_image
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.fn_audit_image_chain_guard();
        """
    )

    # ------------------------------------------------ 账号授权（NFR-C-01）
    # 本地/测试库可能尚未创建 erp_app，缺角色时跳过而不是让迁移失败。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
                REVOKE UPDATE, DELETE, TRUNCATE ON erp.audit_image     FROM erp_app;
                REVOKE UPDATE, DELETE, TRUNCATE ON erp.audit_anchor    FROM erp_app;
                REVOKE UPDATE, DELETE, TRUNCATE ON erp.approval_record FROM erp_app;
                REVOKE UPDATE, DELETE, TRUNCATE ON erp.config_change   FROM erp_app;
                GRANT INSERT, SELECT ON erp.audit_image, erp.audit_anchor,
                      erp.approval_record, erp.config_change TO erp_app;
                GRANT INSERT, SELECT, UPDATE ON erp.credential_lease TO erp_app;
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    # 先删表（触发器随表删除），再删函数，保证可重复执行
    for table in (
        "connector_breaker_state",
        "connector_rate_bucket",
        "connector_idempotency",
        "credential_lease",
        "config_change",
        "approval_record",
        "audit_anchor",
        "audit_image",
        "compensation_task",
    ):
        op.drop_table(table, schema=SCHEMA)

    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.fn_audit_image_chain_guard()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.fn_append_only()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.fn_compensation_seq_guard()")
