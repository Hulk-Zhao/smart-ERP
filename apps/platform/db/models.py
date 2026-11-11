"""核心库表模型（C5 基线）。

只覆盖本里程碑需要的四张表：``job_queue``（C15 队列）与 ``ticket`` /
``ticket_event`` / ``ticket_step``（C3 工单真源）。其余表由各流按迁移窗口追加。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from apps.platform.contracts.ticket import STEP_STATES, TICKET_STATES
from apps.platform.db.base import SCHEMA, Base

QUEUE_STATES: tuple[str, ...] = ("queued", "running", "succeeded", "failed", "dead")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class JobQueue(Base):
    """PG 队列（C15）：入队与业务写同事务，不依赖 Redis。"""

    __tablename__ = "job_queue"
    __table_args__ = (
        CheckConstraint(_in_list("state", QUEUE_STATES), name="ck_job_queue_state"),
        CheckConstraint("attempts >= 0", name="ck_job_queue_attempts_non_negative"),
        Index(
            "job_queue_pick_idx",
            "queue",
            text("priority DESC"),
            "run_at",
            postgresql_where=text("state = 'queued'"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    queue: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'queued'")
    )
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("3")
    )
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Ticket(Base):
    """工单投影表（当前状态、影响面、金额、锁）。"""

    __tablename__ = "ticket"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "scene_code", "business_key", name="uq_ticket_business"
        ),
        CheckConstraint(
            "trigger_kind IN ('timer', 'event', 'manual')", name="ck_ticket_trigger_kind"
        ),
        CheckConstraint(_in_list("state", TICKET_STATES), name="ck_ticket_state"),
        CheckConstraint(
            "write_level IS NULL OR write_level BETWEEN 0 AND 3",
            name="ck_ticket_write_level",
        ),
        CheckConstraint(
            "amount IS NULL OR amount >= 0", name="ck_ticket_amount_non_negative"
        ),
        Index("ticket_tenant_state_created_idx", "tenant_id", "state", text("created_at DESC")),
        Index("ticket_tenant_business_key_idx", "tenant_id", "business_key"),
        # 列表默认按 (created_at DESC, id DESC) 键集分页；上面那条索引第二列是
        # state，不带状态过滤时供不了序（0010）。
        Index(
            "ticket_tenant_created_id_idx",
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_no: Mapped[str] = mapped_column(Text, nullable=False)
    scene_code: Mapped[str] = mapped_column(Text, nullable=False)
    initiator_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(Text, nullable=False)
    business_key: Mapped[str] = mapped_column(Text, nullable=False)
    # API 读路径的 scope 过滤字段（0010）。可空＝「执行器建的历史工单没有这些信息」，
    # 不能用空数组冒充：空数组会让 `systems <@ scope` 恒真。
    org_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    period: Mapped[str | None] = mapped_column(Text, nullable=True)
    systems: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    write_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    diff_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    systems_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    touches_fund: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # 审批通过后只执行这一份写计划；不持久化则进程重启后 approve 无计划可执行。
    pending_write: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class TicketEvent(Base):
    """工单事件真源：append-only + 哈希链 + 迁移白名单触发器。"""

    __tablename__ = "ticket_event"
    __table_args__ = (
        UniqueConstraint("ticket_id", "seq", name="uq_ticket_event_seq"),
        Index("ticket_event_tenant_created_idx", "tenant_id", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.ticket.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class TicketStep(Base):
    """步骤级真源：每步一行，写等级存实际生效值（0..2）。"""

    __tablename__ = "ticket_step"
    __table_args__ = (
        UniqueConstraint("ticket_id", "step_no", name="uq_ticket_step_no"),
        CheckConstraint(_in_list("state", STEP_STATES), name="ck_ticket_step_state"),
        CheckConstraint(
            "write_level BETWEEN 0 AND 2",
            name="ck_ticket_step_write_level_executable",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.ticket.id"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    action_key: Mapped[str] = mapped_column(Text, nullable=False)
    target_system: Mapped[str] = mapped_column(Text, nullable=False)
    write_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    request_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    before_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # API 详情要输出的目标对象（0010）；执行器路径这两列为空。
    object_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 不加外键：API 的审计影像可能只在进程内账本里，外键会让「先写步骤、后落影像」
    # 的两段提交在第一段就失败。
    audit_image_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
