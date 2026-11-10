"""审计链锚点 + 全局写入开关 + 补偿任务租约

Revision ID: 0004_audit_chain_anchor
Revises: 0003_recon_persistence
Create Date: 2026-11-11

来源：``apps/agents/models.py``（``TicketEventAnchor`` / ``AuditGuardState`` /
``CompensationTask.lease_owner|heartbeat_at``）与混沌演练 D3/D4 决策。

- ``erp.ticket_event_anchor``：与每次 ``ticket_event`` 追加同事务更新；
  执行热路径只读一行，比对链尾 seq/hash 做 O(1) fail-closed 校验；
- ``erp.audit_guard_state``：巡检发现断链后把全局写入降级为只读；
- ``erp.compensation_task``：补 ``lease_owner`` / ``heartbeat_at`` 与 stale 扫描
  部分索引，用于回收崩溃遗留的 running（单跑语义仍由状态机 + SKIP LOCKED 保证，
  唯一索引只做兜底）。

升级时对存量事件回填锚点（取每个工单最新一条），否则热路径会因「缺锚点」
把存量工单全部判为断链——回填只是快照，链是否可信仍由首次全链巡检确认。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_audit_chain_anchor"
down_revision = "0003_recon_persistence"
branch_labels = None
depends_on = None

SCHEMA = "erp"


def _assert_event_chains_intact() -> None:
    """回填锚点前做全链重放校验；断链则中止迁移并打印断点位置。

    否则升级会把一段已损坏的链尾直接固化成锚点，问题从此被藏起来，
    热路径再也发现不了。
    """

    from apps.agents.events import EventRecord, ReplayError, replay

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"""
            SELECT ticket_id, seq, event_type, from_state, to_state, payload,
                   trace_id, prev_hash, hash
              FROM {SCHEMA}.ticket_event
             ORDER BY ticket_id, seq
            """
        )
    ).mappings().all()

    grouped: dict[object, list[EventRecord]] = {}
    for row in rows:
        grouped.setdefault(row["ticket_id"], []).append(
            EventRecord(
                seq=row["seq"],
                event_type=row["event_type"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                payload=row["payload"] or {},
                trace_id=row["trace_id"],
                prev_hash=row["prev_hash"],
                hash=row["hash"],
            )
        )

    for ticket_id, events in grouped.items():
        previous: EventRecord | None = None
        for record in events:
            if record.computed_hash != record.hash:
                raise RuntimeError(
                    f"迁移中止：工单 {ticket_id} seq={record.seq} 哈希自校验失败"
                    "（事件被改写），请先修复数据再升级"
                )
            if previous is not None and record.prev_hash != previous.hash:
                raise RuntimeError(
                    f"迁移中止：工单 {ticket_id} seq={record.seq} 的 prev_hash "
                    f"与 seq={previous.seq} 不衔接，请先修复数据再升级"
                )
            previous = record
        try:
            replay(events)
        except ReplayError as exc:
            raise RuntimeError(
                f"迁移中止：工单 {ticket_id} 全链重放失败（断点见下），"
                f"请先修复数据再升级：{exc}"
            ) from exc



def upgrade() -> None:
    op.create_table(
        "ticket_event_anchor",
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            [f"{SCHEMA}.ticket.id"],
            name="fk_ticket_event_anchor_ticket_id_ticket",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ticket_id", name="pk_ticket_event_anchor"),
        schema=SCHEMA,
    )

    op.create_table(
        "audit_guard_state",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("scope", name="pk_audit_guard_state"),
        schema=SCHEMA,
    )

    # 回填前先全链校验：断链时直接中止迁移并打印断点，避免把坏链的尾巴固化成锚点。
    # 离线渲染（--sql / 守卫测试）没有连接可读事件，跳过校验，交由真实升级执行。
    if not op.get_context().as_sql:
        _assert_event_chains_intact()

    # 存量工单锚点回填（只取链尾快照；完整性已在上一步校验）
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.ticket_event_anchor (ticket_id, seq, hash)
        SELECT DISTINCT ON (ticket_id) ticket_id, seq, hash
          FROM {SCHEMA}.ticket_event
         ORDER BY ticket_id, seq DESC
        ON CONFLICT (ticket_id) DO NOTHING
        """
    )

    op.add_column(
        "compensation_task", sa.Column("lease_owner", sa.Text(), nullable=True), schema=SCHEMA
    )
    op.add_column(
        "compensation_task",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "compensation_task_stale_idx",
        "compensation_task",
        ["state", "heartbeat_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("state = 'running'"),
    )

    # 应用账号：锚点/开关要能 upsert，但不得删除（审计证据）。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
                GRANT SELECT, INSERT, UPDATE ON erp.ticket_event_anchor TO erp_app;
                GRANT SELECT, INSERT, UPDATE ON erp.audit_guard_state   TO erp_app;
                REVOKE DELETE, TRUNCATE ON erp.ticket_event_anchor FROM erp_app;
                REVOKE DELETE, TRUNCATE ON erp.audit_guard_state   FROM erp_app;
            END IF;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.drop_index("compensation_task_stale_idx", table_name="compensation_task", schema=SCHEMA)
    op.drop_column("compensation_task", "heartbeat_at", schema=SCHEMA)
    op.drop_column("compensation_task", "lease_owner", schema=SCHEMA)
    op.drop_table("audit_guard_state", schema=SCHEMA)
    op.drop_table("ticket_event_anchor", schema=SCHEMA)
