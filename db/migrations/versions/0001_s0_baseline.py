"""S0 基线：schema erp + job_queue + ticket / ticket_event / ticket_step

Revision ID: 0001_s0_baseline
Revises:
Create Date: 2026-11-10

PG15 语法；不使用 ``any_value()`` / ``MERGE ... RETURNING`` 等 PG16 特性。
状态迁移白名单直接由 ``apps.platform.contracts.ticket.TRANSITIONS`` 生成，
避免「代码改了、DB 触发器忘了改」的双真源漂移。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from apps.platform.contracts.ticket import STEP_STATES, TICKET_STATES, TRANSITION_EDGES
from apps.platform.db.models import QUEUE_STATES

revision = "0001_s0_baseline"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "erp"


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _state_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({_in_list(values)})", name=name)


def _edge_values_sql() -> str:
    rows = [f"('{source}', '{target}')" for source, target in sorted(TRANSITION_EDGES)]
    return ",\n                ".join(rows)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    # ---------------------------------------------------------------- job_queue
    op.create_table(
        "job_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("queue", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.SmallInteger(), nullable=False, server_default=sa.text("3")),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        _state_check("state", QUEUE_STATES, "ck_job_queue_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_job_queue_attempts_non_negative"),
        sa.PrimaryKeyConstraint("id", name="pk_job_queue"),
        schema=SCHEMA,
    )
    op.create_index(
        "job_queue_pick_idx",
        "job_queue",
        ["queue", sa.text("priority DESC"), "run_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("state = 'queued'"),
    )

    # ------------------------------------------------------------------- ticket
    op.create_table(
        "ticket",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_no", sa.Text(), nullable=False),
        sa.Column("scene_code", sa.Text(), nullable=False),
        sa.Column("initiator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column("business_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("diff_amount", sa.Numeric(18, 4), nullable=True),
        sa.Column("systems_count", sa.Integer(), nullable=True),
        sa.Column("touches_fund", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "trigger_kind IN ('timer', 'event', 'manual')", name="ck_ticket_trigger_kind"
        ),
        _state_check("state", TICKET_STATES, "ck_ticket_state"),
        sa.PrimaryKeyConstraint("id", name="pk_ticket"),
        sa.UniqueConstraint("tenant_id", "scene_code", "business_key", name="uq_ticket_business"),
        schema=SCHEMA,
    )
    op.create_index(
        "ticket_tenant_state_created_idx",
        "ticket",
        ["tenant_id", "state", sa.text("created_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ticket_tenant_business_key_idx",
        "ticket",
        ["tenant_id", "business_key"],
        schema=SCHEMA,
    )

    # -------------------------------------------------------------- ticket_event
    op.create_table(
        "ticket_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            [f"{SCHEMA}.ticket.id"],
            name="fk_ticket_event_ticket_id_ticket",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_event"),
        sa.UniqueConstraint("ticket_id", "seq", name="uq_ticket_event_seq"),
        schema=SCHEMA,
    )
    op.create_index(
        "ticket_event_tenant_created_idx",
        "ticket_event",
        ["tenant_id", "created_at"],
        schema=SCHEMA,
    )

    # --------------------------------------------------------------- ticket_step
    op.create_table(
        "ticket_step",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("action_key", sa.Text(), nullable=False),
        sa.Column("target_system", sa.Text(), nullable=False),
        sa.Column("write_level", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("request_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("before_ref", sa.Text(), nullable=True),
        sa.Column("after_ref", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        _state_check("state", STEP_STATES, "ck_ticket_step_state"),
        # W3 不允许落库：写到 3 直接违反约束
        sa.CheckConstraint(
            "write_level BETWEEN 0 AND 2", name="ck_ticket_step_write_level_executable"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            [f"{SCHEMA}.ticket.id"],
            name="fk_ticket_step_ticket_id_ticket",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_step"),
        sa.UniqueConstraint("ticket_id", "step_no", name="uq_ticket_step_no"),
        schema=SCHEMA,
    )

    # ------------------------------------------------- DB 侧第二道闸：事件表护栏
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.fn_ticket_event_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'ticket_event 只允许追加，拒绝 %', TG_OP;
            END IF;
            IF NEW.from_state IS NOT NULL AND NEW.to_state IS NOT NULL THEN
                IF NOT EXISTS (
                    SELECT 1 FROM (VALUES
                        {_edge_values_sql()}
                    ) AS allowed(from_state, to_state)
                    WHERE allowed.from_state = NEW.from_state
                      AND allowed.to_state = NEW.to_state
                ) THEN
                    RAISE EXCEPTION '非法状态迁移: % -> %', NEW.from_state, NEW.to_state;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER ticket_event_guard
        BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.ticket_event
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.fn_ticket_event_guard();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS ticket_event_guard ON {SCHEMA}.ticket_event")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.fn_ticket_event_guard()")
    op.drop_index("ticket_event_tenant_created_idx", table_name="ticket_event", schema=SCHEMA)
    op.drop_table("ticket_event", schema=SCHEMA)
    op.drop_table("ticket_step", schema=SCHEMA)
    op.drop_index("ticket_tenant_business_key_idx", table_name="ticket", schema=SCHEMA)
    op.drop_index("ticket_tenant_state_created_idx", table_name="ticket", schema=SCHEMA)
    op.drop_table("ticket", schema=SCHEMA)
    op.drop_index("job_queue_pick_idx", table_name="job_queue", schema=SCHEMA)
    op.drop_table("job_queue", schema=SCHEMA)
    # schema erp 由初始化脚本创建，downgrade 不删（避免误删同 schema 下其他对象）
