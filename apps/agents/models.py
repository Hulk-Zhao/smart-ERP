"""S3 运行时自有表模型。

- ``erp.compensation_task``：补偿队列（0002 建表，0004 补租约两列）；
- ``erp.ticket_event_anchor``：事件链锚点，热路径 O(1) 校验链尾；
- ``erp.audit_guard_state``：全局写入开关，巡检断链后降级只读；
- ``erp.worker_heartbeat`` / ``erp.scheduler_lease``：C15 常驻进程的落库心跳与
  单点租约（0005 建表），worker/scheduler 存活判定看这里，不看进程内变量。

迁移是唯一真源：``ADDITIONAL_DDL`` 只保留 0002 用到的那两条结构约束，
不再把新增表/列塞进来（否则历史迁移会随模型演进而改变行为）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from apps.platform.db.base import SCHEMA, Base

COMPENSATION_STATES: tuple[str, ...] = ("queued", "running", "done", "failed")


class CompensationTask(Base):
    """独立补偿队列：``compensate_seq`` 取原步骤序号，倒序消费（§3.1.2/§3.1.5）。"""

    __tablename__ = "compensation_task"
    __table_args__ = (
        UniqueConstraint("ticket_id", "compensate_seq", name="uq_compensation_task_seq"),
        CheckConstraint(
            f"state IN ({', '.join(repr(state) for state in COMPENSATION_STATES)})",
            name="ck_compensation_task_state",
        ),
        CheckConstraint("compensate_seq >= 1", name="ck_compensation_task_seq_positive"),
        CheckConstraint("attempts >= 0", name="ck_compensation_task_attempts"),
        Index(
            "compensation_task_dequeue_idx",
            "ticket_id",
            text("compensate_seq DESC"),
        ),
        # stale running 回收：巡检/启动恢复按 (state, heartbeat_at) 扫
        Index(
            "compensation_task_stale_idx",
            "state",
            "heartbeat_at",
            postgresql_where=text("state = 'running'"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.ticket.id"), nullable=False
    )
    original_step: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.ticket_step.id"), nullable=False
    )
    compensate_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'queued'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class TicketEventAnchor(Base):
    """事件链锚点：与每次事件追加同事务更新；热路径只读一行做 O(1) 比对。"""

    __tablename__ = "ticket_event_anchor"
    __table_args__ = {"schema": SCHEMA}

    ticket_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.ticket.id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AuditGuardState(Base):
    """全局写入开关：审计链巡检断链后置 readonly，W0 只读仍可用。"""

    __tablename__ = "audit_guard_state"
    __table_args__ = {"schema": SCHEMA}

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class WorkerHeartbeat(Base):
    """常驻 worker 的落库心跳（C15）：健康探针只看这张表，不看进程内变量。"""

    __tablename__ = "worker_heartbeat"
    __table_args__ = (
        Index("worker_heartbeat_recent_idx", "heartbeat_at"),
        {"schema": SCHEMA},
    )

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    queues: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'starting'")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    jobs_done: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    jobs_failed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )


class SchedulerLease(Base):
    """scheduler 单点租约（C15）：库里可见，过期可被接管。"""

    __tablename__ = "scheduler_lease"
    __table_args__ = {"schema": SCHEMA}

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# 0002 迁移会执行这两条结构约束；新增表/列一律走迁移，不再塞进这里。
ADDITIONAL_DDL: tuple[str, ...] = (
    f"""
    CREATE OR REPLACE FUNCTION {SCHEMA}.fn_compensation_seq_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        expected int;
    BEGIN
        SELECT step_no INTO expected
          FROM {SCHEMA}.ticket_step
         WHERE id = NEW.original_step AND ticket_id = NEW.ticket_id;
        IF expected IS NULL THEN
            RAISE EXCEPTION '补偿任务必须指向同一工单的步骤: step=%', NEW.original_step;
        END IF;
        IF NEW.compensate_seq <> expected THEN
            RAISE EXCEPTION 'compensate_seq 必须等于原步骤序号: 期望 %, 实际 %',
                expected, NEW.compensate_seq;
        END IF;
        RETURN NEW;
    END;
    $$;
    """,
    f"""
    CREATE OR REPLACE TRIGGER compensation_task_seq_guard
    BEFORE INSERT OR UPDATE ON {SCHEMA}.compensation_task
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.fn_compensation_seq_guard();
    """,
    # 同一工单同一时刻只允许一个补偿任务 running（§3.1.5 第 3 条，用索引强制而非代码习惯）
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS compensation_task_one_running_idx
        ON {SCHEMA}.compensation_task (ticket_id)
        WHERE state = 'running';
    """,
)
