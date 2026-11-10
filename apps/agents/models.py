"""S3 运行时自有表模型（待并入 S0 迁移链，见 ``MIGRATION_NEEDS.md``）。

只新增 ``erp.compensation_task`` 一张表：S0 基线（0001）里的 ``ticket`` /
``ticket_event`` / ``ticket_step`` 已满足事件溯源与步骤级断点，无需改动。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
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
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# SQLAlchemy 表达不了的两个结构约束：由迁移脚本补（测试里等价执行，保证本地可跑）
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
