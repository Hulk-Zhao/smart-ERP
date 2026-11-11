"""审批记录的 PostgreSQL 端口（PR4）。

分工：

- ``erp.approval_record`` 是审批意见的**持久真源**；
- 判定仍在 ``ApprovalService``（同步，写闸门 / 访问闸门复用同一入口），
  但每个请求入口先调 :meth:`SqlApprovalStore.list_for` 水合进进程内缓存，
  因此容器重建后判定与 ``GET /v1/tickets/{id}`` 的 ``approvals`` 都来自库；
- 驳回需要「审批行 + 状态推进」原子提交，走
  :func:`add_approval_row`（由 ``TicketStore.transition`` 的 ``before_append``
  在**同一事务**内调用），任何一侧失败都整体回滚。

``approval_record`` 上还有 DB 层的 add-only 触发器与
``UNIQUE(ticket_id, approver_id)``：代码层 ``DuplicateApprover`` 先拒绝，
唯一约束只是并发下的兜底。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.security.approvals import ApprovalRecord, DuplicateApprover
from apps.security.models import ApprovalRecordRow


class ApprovalStorePort(Protocol):
    """审批记录端口；见 :class:`SqlApprovalStore` 与内存模式的 ``None``。"""

    async def add(self, *, tenant_id: UUID, record: ApprovalRecord) -> None: ...

    async def list_for(
        self, *, tenant_id: UUID, ticket_id: UUID
    ) -> list[ApprovalRecord]: ...


def add_approval_row(
    session: AsyncSession, *, tenant_id: UUID, record: ApprovalRecord
) -> None:
    """在调用方事务内插入审批行（驳回与状态推进同事务时复用）。"""

    session.add(
        ApprovalRecordRow(
            id=UUID(record.id),
            tenant_id=tenant_id,
            ticket_id=UUID(record.ticket_id),
            role=record.role,
            approver_id=record.approver_id,
            initiator_id=record.initiator_id,
            approver_roles=list(record.approver_roles),
            decision=record.decision,
            comment=record.comment,
            exception_reason=record.exception_reason,
            decided_at=_as_utc(record.decided_at),
        )
    )


class SqlApprovalStore:
    """惰性建引擎；导入/装配阶段不连库（与 ``SqlTicketRepository`` 同一原则）。"""

    def __init__(self, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory

    def _factory(self) -> Any:
        if self._session_factory is None:
            from apps.platform.db.session import build_engine, build_session_factory

            self._session_factory = build_session_factory(build_engine())
        return self._session_factory

    async def add(self, *, tenant_id: UUID, record: ApprovalRecord) -> None:
        try:
            async with self._factory()() as session, session.begin():
                add_approval_row(session, tenant_id=tenant_id, record=record)
        except IntegrityError as exc:
            # 并发下两人同时提交：代码层已从水合后的缓存里查过，这里是兜底。
            raise DuplicateApprover(
                f"{record.approver_id} 已在工单 {record.ticket_id} 留下审批记录"
                "（复核人 ≠ 审批人）"
            ) from exc

    async def list_for(
        self, *, tenant_id: UUID, ticket_id: UUID
    ) -> list[ApprovalRecord]:
        stmt = (
            sa.select(ApprovalRecordRow)
            .where(
                ApprovalRecordRow.tenant_id == tenant_id,
                ApprovalRecordRow.ticket_id == ticket_id,
            )
            .order_by(ApprovalRecordRow.decided_at, ApprovalRecordRow.id)
        )
        async with self._factory()() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]


def _to_record(row: ApprovalRecordRow) -> ApprovalRecord:
    return ApprovalRecord(
        id=str(row.id),
        ticket_id=str(row.ticket_id),
        role=row.role,
        approver_id=row.approver_id,
        initiator_id=row.initiator_id,
        approver_roles=tuple(row.approver_roles or ()),
        decision=row.decision,  # type: ignore[arg-type]
        comment=row.comment,
        decided_at=_as_utc(row.decided_at),
        exception_reason=row.exception_reason,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = ["ApprovalStorePort", "SqlApprovalStore", "add_approval_row"]
