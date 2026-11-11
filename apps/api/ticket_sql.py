"""工单投影的 PostgreSQL 读写路径（PR2 读 / PR3 写）。

读：与内存 ``TicketRepository`` 同语义——租户是查询条件（不是取到再判断）、
scope 过滤落在 SQL 里、键集游标沿用 ``{"t": iso, "i": uuid}``。

写：全部经 :class:`apps.agents.store.TicketStore`，事件与投影同事务；
``add`` / ``save`` 仍显式拒绝，避免任何入口绕过事件直接改投影：

- 建单 → ``create`` → ``TicketStore.create_ticket``（created 事件）；
- 状态推进 → ``transition`` → ``TicketStore.transition``（transition 事件）；
- 执行完的步骤 → ``record_step`` → ``TicketStore.record_step``（step 事件）；
- ``save`` 只被 ``compensate_ticket`` 使用；同步回滚本期关闭，SQL 模式下一律
  fail-closed（503），且发生在任何连接器调用之前。

``version`` 对外是「建单即 1」，DB 里存的是迁移次数（建单 0）。两个实现
（内存/SQL）都在端口边界做同样的 0→1 偏移，``ActionOut.version`` 因此不变。

``systems IS NULL`` 的语义：**不可见**（fail-closed）。NULL 表示「涉及系统未知」，
不是「全部」：列表用显式的 ``systems IS NOT NULL`` 与 ``@>`` 两个条件，详情在
service 层抛 403；两处都不允许 ``IS NULL OR ...`` 的短路把未知放行成全部可见。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from apps.agents.state_machine import TransitionContext
from apps.agents.store import TicketNotFound, TicketStore
from apps.api.approval_sql import add_approval_row
from apps.api.pagination import decode_cursor, encode_cursor
from apps.api.store import PendingWrite, TicketRecord, TicketStepRecord
from apps.platform.api.errors import ProblemError
from apps.platform.contracts.errors import ErrorCode
from apps.platform.contracts.ticket import TicketState
from apps.platform.db.models import Ticket, TicketStep
from apps.platform.policy.levels import WriteLevel
from apps.security.approvals import ApprovalRecord
from apps.security.scope import Scope


class TicketStorePort(Protocol):
    """容器注入的工单端口；内存实现见 ``apps.api.store.TicketRepository``。"""

    def add(self, ticket: TicketRecord) -> TicketRecord: ...

    async def create(self, ticket: TicketRecord) -> TicketRecord: ...

    async def get(self, *, tenant_id: UUID, ticket_id: UUID) -> TicketRecord | None: ...

    async def save(self, ticket: TicketRecord) -> None: ...

    async def transition(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        target: TicketState,
        trigger: str,
        context: TransitionContext | None = None,
        actor_id: UUID | None = None,
        trace_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        approval_record: ApprovalRecord | None = None,
    ) -> int: ...

    async def record_step(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        step: TicketStepRecord,
        request_hash: str,
        trace_id: str | None = None,
    ) -> None: ...

    async def list(
        self,
        *,
        tenant_id: UUID,
        scope: Scope,
        state: str | None = None,
        scene_code: str | None = None,
        business_key: str | None = None,
        ticket_no: str | None = None,
        initiator_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[TicketRecord], str | None]: ...


class SqlTicketRepository:
    """PostgreSQL 适配器：首次使用时才建引擎，导入/装配阶段不连库。"""

    def __init__(self, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory
        self._store: TicketStore | None = None

    def _factory(self) -> Any:
        if self._session_factory is None:
            from apps.platform.db.session import build_engine, build_session_factory

            self._session_factory = build_session_factory(build_engine())
        return self._session_factory

    def _events(self) -> TicketStore:
        if self._store is None:
            self._store = TicketStore(self._factory())
        return self._store

    def add(self, ticket: TicketRecord) -> TicketRecord:
        raise NotImplementedError(
            "SQL 模式请用 await create(...)（ticket_event 为真源）；add 只属于内存测试仓储"
        )

    async def save(self, ticket: TicketRecord) -> None:
        # 只有 compensate_ticket 会走到这里：同步回滚本期关闭，SQL 模式下必须
        # fail-closed，且在连接器调用之前——不能靠「直写投影」制造链外状态。
        raise ProblemError(
            ErrorCode.ROLLBACK_UNAVAILABLE,
            "同步回滚尚未接入事件化写（SQL 模式）：本次未调用任何连接器，工单状态未改变。",
        )

    async def create(self, ticket: TicketRecord) -> TicketRecord:
        created = await self._events().create_ticket(
            tenant_id=ticket.tenant_id,
            ticket_no=ticket.ticket_no,
            scene_code=ticket.scene_code,
            initiator_id=ticket.initiator_id,
            trigger_kind="manual",
            business_key=ticket.business_key,
            org_id=ticket.org_id or None,
            period=ticket.period or None,
            systems=ticket.systems,
            write_level=int(ticket.write_level)
            if ticket.write_level is not None
            else None,
            amount=ticket.amount,
            systems_count=(len(ticket.systems) if ticket.systems is not None else None),
            touches_fund=ticket.touches_fund,
            pending_write=_pending_payload(ticket.pending_write),
        )
        fetched = await self.get(tenant_id=ticket.tenant_id, ticket_id=created.id)
        if fetched is None:  # pragma: no cover - 刚提交的行不可能读不到
            raise TicketNotFound(f"工单不存在: {created.id}")
        return fetched

    async def transition(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        target: TicketState,
        trigger: str,
        context: TransitionContext | None = None,
        actor_id: UUID | None = None,
        trace_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        approval_record: ApprovalRecord | None = None,
    ) -> int:
        # tenant 是查询条件：先钉住归属，再进事件事务（避免「改完再发现不是本租户」）。
        async with self._factory()() as session:
            owned = await session.scalar(
                sa.select(Ticket.id).where(
                    Ticket.id == ticket_id, Ticket.tenant_id == tenant_id
                )
            )
        if owned is None:
            raise TicketNotFound(f"工单不存在: {ticket_id}")

        before_append: Callable[[AsyncSession], None] | None = None
        if approval_record is not None:
            # 驳回决定与状态推进同一事务：见 ``TicketStore.transition`` 的钩子。

            def _insert_approval(session: AsyncSession) -> None:
                add_approval_row(session, tenant_id=tenant_id, record=approval_record)

            before_append = _insert_approval

        await self._events().transition(
            ticket_id,
            target,
            trigger,
            context=context,
            actor_id=actor_id,
            trace_id=trace_id,
            payload=payload,
            before_append=before_append,
        )
        async with self._factory()() as session:
            stored = await session.scalar(
                sa.select(Ticket.version).where(
                    Ticket.id == ticket_id, Ticket.tenant_id == tenant_id
                )
            )
        if stored is None:
            raise TicketNotFound(f"工单不存在: {ticket_id}")
        return int(stored) + 1

    async def record_step(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        step: TicketStepRecord,
        request_hash: str,
        trace_id: str | None = None,
    ) -> None:
        # tenant 是查询条件：先在端口上钉住归属，再进事件事务。
        async with self._factory()() as session:
            owned = await session.scalar(
                sa.select(Ticket.id).where(
                    Ticket.id == ticket_id, Ticket.tenant_id == tenant_id
                )
            )
        if owned is None:
            raise TicketNotFound(f"工单不存在: {ticket_id}")
        await self._events().record_step(
            ticket_id=ticket_id,
            step_id=step.step_id,
            step_no=step.step_no,
            action_key=step.action_key,
            target_system=step.system,
            write_level=int(step.level),
            idem_key=step.idem_key,
            request_hash=request_hash,
            state=step.state,
            object_type=step.object_type or None,
            object_id=step.object_id or None,
            audit_image_id=step.audit_image_id,
            executed_at=step.executed_at,
            trace_id=trace_id,
        )

    async def get(self, *, tenant_id: UUID, ticket_id: UUID) -> TicketRecord | None:
        factory = self._factory()
        async with factory() as session:
            row = (
                await session.execute(
                    sa.select(Ticket).where(
                        Ticket.id == ticket_id, Ticket.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            step_rows = (
                (
                    await session.execute(
                        sa.select(TicketStep)
                        .where(TicketStep.ticket_id == ticket_id)
                        .order_by(TicketStep.step_no)
                    )
                )
                .scalars()
                .all()
            )
        return _to_record(row, [_to_step(step) for step in step_rows])

    async def list(
        self,
        *,
        tenant_id: UUID,
        scope: Scope,
        state: str | None = None,
        scene_code: str | None = None,
        business_key: str | None = None,
        ticket_no: str | None = None,
        initiator_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[TicketRecord], str | None]:
        stmt = sa.select(Ticket).where(
            Ticket.tenant_id == tenant_id, *_scope_where(scope)
        )
        if state is not None:
            stmt = stmt.where(Ticket.state == state)
        if scene_code is not None:
            stmt = stmt.where(Ticket.scene_code == scene_code)
        if business_key is not None:
            stmt = stmt.where(Ticket.business_key == business_key)
        if ticket_no is not None:
            stmt = stmt.where(Ticket.ticket_no == ticket_no)
        if initiator_id is not None:
            stmt = stmt.where(Ticket.initiator_id == initiator_id)
        if cursor is not None:
            payload = decode_cursor(cursor)
            after_created = datetime.fromisoformat(str(payload["t"]))
            after_id = UUID(str(payload["i"]))
            # 行比较必须带类型：``i`` 若按文本比较，uuid 与 hex 串的排序在这里
            # 恰好一致，但同秒数据的游标会变得依赖隐式转换，丢行且不报错。
            stmt = stmt.where(
                sa.tuple_(Ticket.created_at, Ticket.id)
                < sa.tuple_(
                    sa.literal(after_created, sa.DateTime(timezone=True)),
                    sa.literal(after_id, PGUUID(as_uuid=True)),
                )
            )
        stmt = stmt.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(
            limit + 1
        )

        factory = self._factory()
        async with factory() as session:
            rows = (await session.execute(stmt)).scalars().all()

        window = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = window[-1]
            next_cursor = encode_cursor(
                {"t": last.created_at.isoformat(), "i": str(last.id)}
            )
        # 列表不加载步骤：``TicketOut`` 不输出 steps，详情（get）才需要；
        # 双实现一致性用例在列表侧只比对投影字段，get 侧才比对 steps。
        return [_to_record(row, []) for row in window], next_cursor


def _scope_where(scope: Scope) -> tuple[sa.ColumnElement[bool], ...]:
    return (
        Ticket.org_id == scope.org_id,
        Ticket.period == scope.period,
        # fail-closed：未知范围不参与任何 scope。显式写出，不依赖 ``@> NULL``
        # 求值为 NULL 的副作用。右值必须 CAST 成 text[]，否则退化成 varchar[]
        # 与列类型不匹配（PostgreSQL 不认跨类型的数组运算符）。
        Ticket.systems.is_not(None),
        Ticket.systems.op("<@", is_comparison=True)(
            sa.cast(array(list(scope.system_ids)), ARRAY(sa.Text))
        ),
    )


def _to_record(row: Ticket, steps: list[TicketStepRecord]) -> TicketRecord:
    return TicketRecord(
        ticket_id=row.id,
        tenant_id=row.tenant_id,
        ticket_no=row.ticket_no,
        scene_code=row.scene_code,
        initiator_id=row.initiator_id,
        business_key=row.business_key,
        # 下面两列对执行器历史行可能为 NULL：这类行 systems 也为 NULL，永远不可见，
        # 空串只用于让 get 能映射出记录、好统一走「范围未知」403，不参与输出。
        org_id=row.org_id or "",
        period=row.period or "",
        systems=tuple(row.systems) if row.systems is not None else None,
        write_level=(
            WriteLevel.parse(row.write_level) if row.write_level is not None else None
        ),
        amount=row.amount,
        touches_fund=row.touches_fund,
        state=TicketState(row.state),
        # DB 存迁移次数（建单 0），对外统一 +1：前端乐观并发的起点是 1。
        version=row.version + 1,
        created_at=row.created_at,
        updated_at=row.updated_at,
        pending_write=_to_pending(row.pending_write),
        steps=steps,
    )


def _pending_payload(pending: PendingWrite | None) -> dict[str, Any] | None:
    if pending is None:
        return None
    return {
        "action_key": pending.action_key,
        "system": pending.system,
        "payload": dict(pending.payload),
        "idem_key": pending.idem_key,
    }


def _to_pending(raw: Any) -> PendingWrite | None:
    # 空对象与 NULL 同义：没有锁定的写计划。其余缺键属于坏数据，直接 KeyError。
    if not raw:
        return None
    return PendingWrite(
        action_key=str(raw["action_key"]),
        system=str(raw["system"]),
        payload=dict(raw.get("payload") or {}),
        idem_key=str(raw["idem_key"]),
    )


def _to_step(row: TicketStep) -> TicketStepRecord:
    return TicketStepRecord(
        step_id=row.id,
        step_no=row.step_no,
        action_key=row.action_key,
        system=row.target_system,
        # 执行器步骤没有目标对象列（0010 之前的历史行）：空串而不是编造值；
        # API 自己的步骤一定有值。
        object_type=row.object_type or "",
        object_id=row.object_id or "",
        level=WriteLevel.parse(row.write_level),
        idem_key=row.idempotency_key,
        state=row.state,
        executed_at=row.executed_at,
        audit_image_id=row.audit_image_id,
    )
