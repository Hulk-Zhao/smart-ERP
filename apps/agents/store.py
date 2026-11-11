"""工单事件真源与投影的持久化（SQLAlchemy 2.0 async / PG15）。

约定：
- 只有本模块可以写 ``ticket`` / ``ticket_event`` / ``ticket_step``；
- 每次事件追加都先对 ``ticket`` 行加锁，``seq`` 与哈希链因此天然串行，不会分叉；
- ``ticket`` 的 ``state`` / ``version`` 是投影，任何时刻可由 :meth:`rebuild_projection` 复算。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.agents.events import (
    COMPENSATION_EVENT,
    CREATED_EVENT,
    RECOVERED_EVENT,
    STEP_EVENT,
    TRANSITION_EVENT,
    EventRecord,
    Projection,
    make_event,
    replay,
)
from apps.agents.models import TicketPlanStep
from apps.agents.state_machine import TransitionContext, resolve_transition
from apps.platform.contracts.ticket import TicketState
from apps.platform.db.models import Ticket, TicketEvent, TicketStep


class StoreError(Exception):
    """持久化层拒绝操作。"""


class TicketNotFound(StoreError):
    pass


class DuplicateTicket(StoreError):
    """``UNIQUE(tenant_id, scene_code, business_key)`` 命中（FR-1101 幂等）。"""


class DuplicateStep(StoreError):
    """同一工单内 step_no 重复（断点续跑不得重放已存在的步骤）。"""


#: 「已解决」口径（README §6.1.4）：``COMPLETED``（业务成功闭合）与
#: ``ROLLED_BACK``（补偿成功撤销）必须分开列出——前者外部副作用保留，后者已抵消，
#: 不能合成一个 ``closed`` 概念。``TERMINATED`` / ``REJECTED`` 是零写入或策略拒绝，
#: 外部没有残留，同样算解决。其余（含 ``MANUAL_REQUIRED`` 失败没人接住、
#: ``LOCKED`` 外部已生效待对账）一律计入未解决。
RESOLVED_TICKET_STATES: tuple[str, ...] = (
    TicketState.COMPLETED.value,
    TicketState.ROLLED_BACK.value,
    TicketState.TERMINATED.value,
    TicketState.REJECTED.value,
)


@dataclass(frozen=True, slots=True)
class ProjectionCheck:
    ticket_id: UUID
    stored_state: TicketState
    replay_state: TicketState
    stored_version: int
    replay_version: int

    @property
    def consistent(self) -> bool:
        return (
            self.stored_state is self.replay_state
            and self.stored_version == self.replay_version
        )


@dataclass(frozen=True, slots=True)
class PlanStep:
    """持久化后的单步计划（与执行记录的 ``ticket_step`` 不同：这是输入，不是结果）。"""

    step_no: int
    action_key: str
    target_system: str
    payload: Mapping[str, Any]
    idem_key: str
    verify_payload: Mapping[str, Any] | None = None


def _plan_signature(step: PlanStep) -> tuple[Any, ...]:
    return (
        step.step_no,
        step.action_key,
        step.target_system,
        step.idem_key,
        dict(step.payload),
        dict(step.verify_payload) if step.verify_payload is not None else None,
    )


def _plan_from_row(row: TicketPlanStep) -> PlanStep:
    return PlanStep(
        step_no=row.step_no,
        action_key=row.action_key,
        target_system=row.target_system,
        payload=dict(row.payload),
        idem_key=row.idem_key,
        verify_payload=(
            dict(row.verify_payload) if row.verify_payload is not None else None
        ),
    )


def _record_from_row(row: TicketEvent) -> EventRecord:
    return EventRecord(
        seq=row.seq,
        event_type=row.event_type,
        from_state=row.from_state,
        to_state=row.to_state,
        payload=dict(row.payload or {}),
        actor_id=str(row.actor_id) if row.actor_id else None,
        trace_id=row.trace_id,
        prev_hash=row.prev_hash,
        hash=row.hash,
    )


def _context_payload(context: TransitionContext | None) -> dict[str, Any]:
    if context is None:
        return {}
    payload: dict[str, Any] = {}
    for name in TransitionContext.__dataclass_fields__:
        value = getattr(context, name)
        if isinstance(value, Decimal):
            payload[name] = str(value)
        elif isinstance(value, tuple):
            payload[name] = list(value)
        else:
            payload[name] = value
    return payload


class TicketStore:
    """事件追加 + 投影读写。调用方负责业务判断，本类只保证一致性。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._sf

    # ------------------------------------------------------------------ 工单
    async def create_ticket(
        self,
        *,
        tenant_id: UUID,
        ticket_no: str,
        scene_code: str,
        initiator_id: UUID,
        trigger_kind: str,
        business_key: str,
        actor_id: UUID | None = None,
        trace_id: str | None = None,
        diff_amount: Decimal | None = None,
        systems_count: int | None = None,
        touches_fund: bool = False,
        org_id: str | None = None,
        period: str | None = None,
        systems: Sequence[str] | None = None,
        write_level: int | None = None,
        amount: Decimal | None = None,
        pending_write: Mapping[str, Any] | None = None,
    ) -> Ticket:
        """建单：ticket 投影 + created 事件同事务。

        ``org_id`` / ``period`` / ``systems`` / ``write_level`` / ``amount`` /
        ``pending_write`` 是 API 读路径的投影字段，同时镜像进 created payload——
        否则 ``rebuild_projection`` 只能重建执行器写的那部分，API 建的单重建不出来。
        执行器调用方不传这些参数，行为与 0010 之前一致（DB 为 NULL、payload 无这些键）。
        """

        ticket = Ticket(
            id=uuid4(),
            tenant_id=tenant_id,
            ticket_no=ticket_no,
            scene_code=scene_code,
            initiator_id=initiator_id,
            trigger_kind=trigger_kind,
            business_key=business_key,
            org_id=org_id,
            period=period,
            systems=list(systems) if systems is not None else None,
            state=TicketState.PLANNED.value,
            version=0,
            write_level=write_level,
            amount=amount,
            diff_amount=diff_amount,
            systems_count=(
                systems_count
                if systems_count is not None
                else (len(systems) if systems is not None else None)
            ),
            touches_fund=touches_fund,
            pending_write=dict(pending_write) if pending_write is not None else None,
        )
        payload: dict[str, Any] = {
            "scene_code": scene_code,
            "business_key": business_key,
        }
        if org_id is not None:
            payload["org_id"] = org_id
        if period is not None:
            payload["period"] = period
        if systems is not None:
            payload["systems"] = list(systems)
        if write_level is not None:
            payload["write_level"] = write_level
        if amount is not None:
            payload["amount"] = str(amount)
        if pending_write is not None:
            payload["pending_write"] = dict(pending_write)

        try:
            async with self._sf() as session, session.begin():
                session.add(ticket)
                await session.flush()
                await self._append_locked(
                    session,
                    ticket,
                    event_type=CREATED_EVENT,
                    from_state=None,
                    to_state=TicketState.PLANNED.value,
                    payload=payload,
                    actor_id=actor_id,
                    trace_id=trace_id,
                )
        except IntegrityError as exc:
            raise DuplicateTicket(
                f"同业务键已有工单: {tenant_id}/{scene_code}/{business_key}"
            ) from exc
        return ticket

    async def get_ticket(self, ticket_id: UUID) -> Ticket:
        async with self._sf() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFound(f"工单不存在: {ticket_id}")
            return ticket

    async def count_unresolved(self) -> int:
        """全库未解决工单数；口径见 :data:`RESOLVED_TICKET_STATES`。

        采集是旁路：查询失败直接向上抛，由调用方决定「缺样本」，绝不返回 0。
        """

        async with self._sf() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.state.notin_(RESOLVED_TICKET_STATES))
            )
        return int(count or 0)

    async def transition(
        self,
        ticket_id: UUID,
        target: TicketState | str,
        trigger: str,
        *,
        context: TransitionContext | None = None,
        actor_id: UUID | None = None,
        trace_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        before_append: Callable[[AsyncSession], None] | None = None,
    ) -> EventRecord:
        """校验 + 追加事件 + 更新投影，同一事务内完成。

        ``before_append`` 在锁定工单行、校验通过之后、追加事件之前执行，
        用于把「必须与本次状态推进同生共死」的行写进同一事务（例如驳回决定
        对应的 ``approval_record``）。默认 ``None``，执行器路径行为不变。
        """

        async with self._sf() as session, session.begin():
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFound(f"工单不存在: {ticket_id}")

            target_state = TicketState(target)
            resolve_transition(ticket.state, target_state, trigger, context)

            if before_append is not None:
                before_append(session)

            body: dict[str, Any] = dict(payload or {})
            body["trigger"] = trigger
            body["context"] = _context_payload(context)
            record = await self._append_locked(
                session,
                ticket,
                event_type=TRANSITION_EVENT,
                from_state=ticket.state,
                to_state=target_state.value,
                payload=body,
                actor_id=actor_id,
                trace_id=trace_id,
            )
            ticket.state = target_state.value
            ticket.version = int(ticket.version) + 1
            # 写计划也是投影的一部分：payload 里显式带 pending_write 时同步覆盖，
            # 与 replay() 的口径一致（在线投影和「从事件重建」必须是同一份事实）。
            if "pending_write" in body:
                ticket.pending_write = body["pending_write"]
            ticket.updated_at = datetime.now(UTC)
        return record

    async def record_step(
        self,
        *,
        ticket_id: UUID,
        step_id: UUID,
        step_no: int,
        action_key: str,
        target_system: str,
        write_level: int,
        idem_key: str,
        request_hash: str,
        state: str = "succeeded",
        request_snapshot: Mapping[str, Any] | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        audit_image_id: UUID | None = None,
        executed_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> TicketStep:
        """记录 API 已执行完成的步骤（外部写入已发生），并追加 step 事件。

        与执行器的 ``intent_step``/``mark_step`` 两段提交不同：API 的写计划在
        审批通过时一次性执行完，这里只在成功之后落一行 + 一条事件，语义与改造前
        ``ticket.steps.append`` 一致。``step_id`` 由调用方生成并先用于审计影像，
        因此必须由调用方传入而不是这里 ``uuid4()``，否则影像与步骤对不上。
        """

        if write_level > 2:
            raise StoreError("ticket_step.write_level 只允许 0..2（W3 不可能落库）")

        step = TicketStep(
            id=step_id,
            ticket_id=ticket_id,
            step_no=step_no,
            action_key=action_key,
            target_system=target_system,
            write_level=write_level,
            state=state,
            idempotency_key=idem_key,
            request_hash=request_hash,
            request_snapshot=dict(request_snapshot) if request_snapshot else None,
            object_type=object_type,
            object_id=object_id,
            audit_image_id=audit_image_id,
            executed_at=executed_at,
            attempt=1,
        )
        try:
            async with self._sf() as session, session.begin():
                ticket = await session.scalar(
                    select(Ticket).where(Ticket.id == ticket_id).with_for_update()
                )
                if ticket is None:
                    raise TicketNotFound(f"工单不存在: {ticket_id}")
                session.add(step)
                await session.flush()
                await self._append_locked(
                    session,
                    ticket,
                    event_type=STEP_EVENT,
                    from_state=None,
                    to_state=None,
                    payload={
                        "step_no": step_no,
                        "state": state,
                        "action_key": action_key,
                    },
                    actor_id=None,
                    trace_id=trace_id,
                )
                ticket.updated_at = datetime.now(UTC)
        except IntegrityError as exc:
            raise DuplicateStep(f"工单 {ticket_id} 的 step_no={step_no} 已存在") from exc
        return step

    async def list_events(self, ticket_id: UUID) -> list[EventRecord]:
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(TicketEvent)
                    .where(TicketEvent.ticket_id == ticket_id)
                    .order_by(TicketEvent.seq)
                )
            ).all()
        return [_record_from_row(row) for row in rows]

    async def list_events_bulk(
        self, ticket_ids: Sequence[UUID] | None = None
    ) -> dict[UUID, list[EventRecord]]:
        """一次查询取多工单的全部事件（全链巡检/出表校验用，避免 N+1）。"""

        stmt = select(TicketEvent).order_by(TicketEvent.ticket_id, TicketEvent.seq)
        if ticket_ids is not None:
            ids = list(ticket_ids)
            if not ids:
                return {}
            stmt = stmt.where(TicketEvent.ticket_id.in_(ids))
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        grouped: dict[UUID, list[EventRecord]] = {}
        for row in rows:
            grouped.setdefault(row.ticket_id, []).append(_record_from_row(row))
        return grouped

    async def count_events(self, ticket_ids: Sequence[UUID] | None = None) -> int:
        """统计事件条数（出表闸门先估规模，超上限直接拒绝，避免整批加载）。"""

        stmt = select(func.count()).select_from(TicketEvent)
        if ticket_ids is not None:
            ids = list(ticket_ids)
            if not ids:
                return 0
            stmt = stmt.where(TicketEvent.ticket_id.in_(ids))
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def rebuild_projection(self, ticket_id: UUID) -> Projection:
        """复算投影（只读）；事件损坏时抛 ``ReplayError``。"""

        return replay(await self.list_events(ticket_id))

    async def check_projection(self, ticket_id: UUID) -> ProjectionCheck:
        async with self._sf() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFound(f"工单不存在: {ticket_id}")
            stored_state, stored_version = TicketState(ticket.state), int(ticket.version)
        projection = await self.rebuild_projection(ticket_id)
        return ProjectionCheck(
            ticket_id=ticket_id,
            stored_state=stored_state,
            replay_state=projection.state,
            stored_version=stored_version,
            replay_version=projection.version,
        )

    async def repair_projection(self, ticket_id: UUID) -> ProjectionCheck:
        """投影损坏时按事件重建（不是数据恢复，投影不是真源）。"""

        check = await self.check_projection(ticket_id)
        if check.consistent:
            return check
        async with self._sf() as session, session.begin():
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFound(f"工单不存在: {ticket_id}")
            ticket.state = check.replay_state.value
            ticket.version = check.replay_version
        return check

    # ------------------------------------------------------------------ 步骤
    async def intent_step(
        self,
        *,
        ticket_id: UUID,
        step_no: int,
        action_key: str,
        target_system: str,
        write_level: int,
        idem_key: str,
        request_hash: str,
        request_snapshot: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> TicketStep:
        """两段提交第一段：先落意图（本地事务提交），再调外部。"""

        if write_level > 2:
            raise StoreError("ticket_step.write_level 只允许 0..2（W3 不可能落库）")

        step = TicketStep(
            id=uuid4(),
            ticket_id=ticket_id,
            step_no=step_no,
            action_key=action_key,
            target_system=target_system,
            write_level=write_level,
            state="intent",
            idempotency_key=idem_key,
            request_hash=request_hash,
            request_snapshot=dict(request_snapshot) if request_snapshot else None,
            attempt=1,
        )
        try:
            async with self._sf() as session, session.begin():
                ticket = await session.scalar(
                    select(Ticket).where(Ticket.id == ticket_id).with_for_update()
                )
                if ticket is None:
                    raise TicketNotFound(f"工单不存在: {ticket_id}")
                session.add(step)
                await session.flush()
                await self._append_locked(
                    session,
                    ticket,
                    event_type=STEP_EVENT,
                    from_state=None,
                    to_state=None,
                    payload={
                        "step_no": step_no,
                        "state": "intent",
                        "action_key": action_key,
                        "idempotency_key": idem_key,
                    },
                    actor_id=None,
                    trace_id=trace_id,
                )
        except IntegrityError as exc:
            raise DuplicateStep(f"工单 {ticket_id} 的 step_no={step_no} 已存在") from exc
        return step

    async def mark_step(
        self,
        *,
        step_id: UUID,
        state: str,
        response_snapshot: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        latency_ms: int | None = None,
        before_ref: str | None = None,
        after_ref: str | None = None,
        trace_id: str | None = None,
    ) -> TicketStep:
        async with self._sf() as session, session.begin():
            step = await session.scalar(
                select(TicketStep).where(TicketStep.id == step_id).with_for_update()
            )
            if step is None:
                raise StoreError(f"步骤不存在: {step_id}")
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == step.ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFound(f"工单不存在: {step.ticket_id}")
            step.state = state
            if response_snapshot is not None:
                step.response_snapshot = dict(response_snapshot)
            if error_code is not None:
                step.error_code = error_code
            if latency_ms is not None:
                step.latency_ms = latency_ms
            if before_ref is not None:
                step.before_ref = before_ref
            if after_ref is not None:
                step.after_ref = after_ref
            await self._append_locked(
                session,
                ticket,
                event_type=STEP_EVENT,
                from_state=None,
                to_state=None,
                payload={
                    "step_no": step.step_no,
                    "state": state,
                    "action_key": step.action_key,
                    "error_code": error_code,
                },
                actor_id=None,
                trace_id=trace_id,
            )
        return step

    async def list_steps(self, ticket_id: UUID) -> list[TicketStep]:
        async with self._sf() as session:
            return list(
                (
                    await session.scalars(
                        select(TicketStep)
                        .where(TicketStep.ticket_id == ticket_id)
                        .order_by(TicketStep.step_no)
                    )
                ).all()
            )

    async def get_step(self, step_id: UUID) -> TicketStep:
        async with self._sf() as session:
            step = await session.get(TicketStep, step_id)
            if step is None:
                raise StoreError(f"步骤不存在: {step_id}")
            return step

    async def next_step_no(self, ticket_id: UUID) -> int:
        async with self._sf() as session:
            current = await session.scalar(
                select(func.max(TicketStep.step_no)).where(
                    TicketStep.ticket_id == ticket_id
                )
            )
        return int(current or 0) + 1

    # ------------------------------------------------------------------ 计划
    async def save_plan(self, ticket_id: UUID, steps: Sequence[PlanStep]) -> None:
        """执行前把整份计划落库；重复执行必须是同一份计划，否则拒绝覆盖。

        计划是「一次定稿」的输入：允许同一份计划被重复执行（幂等），但不允许
        中途换成另一份——否则执行记录与库里计划会背离，恢复入口续跑的就是假计划。
        """
        if not steps:
            raise StoreError("执行计划不能为空")
        wanted = sorted((_plan_signature(step) for step in steps), key=lambda item: item[0])

        async with self._sf() as session, session.begin():
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFound(str(ticket_id))
            rows = (
                await session.scalars(
                    select(TicketPlanStep)
                    .where(TicketPlanStep.ticket_id == ticket_id)
                    .order_by(TicketPlanStep.step_no)
                )
            ).all()
            if rows:
                stored = [_plan_signature(_plan_from_row(row)) for row in rows]
                if stored != wanted:
                    raise StoreError(
                        f"工单 {ticket_id} 已持久化另一份执行计划，拒绝覆盖："
                        "计划的变更必须走新工单"
                    )
                return
            session.add_all(
                [
                    TicketPlanStep(
                        ticket_id=ticket_id,
                        step_no=step.step_no,
                        action_key=step.action_key,
                        target_system=step.target_system,
                        payload=dict(step.payload),
                        idem_key=step.idem_key,
                        verify_payload=(
                            dict(step.verify_payload)
                            if step.verify_payload is not None
                            else None
                        ),
                    )
                    for step in steps
                ]
            )

    async def load_plan(self, ticket_id: UUID) -> list[PlanStep]:
        """按 ``step_no`` 读回持久化计划；0013 之前执行的工单返回空列表。"""
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(TicketPlanStep)
                    .where(TicketPlanStep.ticket_id == ticket_id)
                    .order_by(TicketPlanStep.step_no)
                )
            ).all()
        return [_plan_from_row(row) for row in rows]

    # ---------------------------------------------------------------- 内部
    async def _append_locked(
        self,
        session: AsyncSession,
        ticket: Ticket,
        *,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        payload: Mapping[str, Any],
        actor_id: UUID | None,
        trace_id: str | None,
    ) -> EventRecord:
        """调用方必须已持有 ``ticket`` 行锁（seq/哈希链的分叉防线）。"""

        last = await session.scalar(
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket.id)
            .order_by(TicketEvent.seq.desc())
            .limit(1)
        )
        record = make_event(
            _record_from_row(last) if last is not None else None,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            payload=payload,
            actor_id=str(actor_id) if actor_id else None,
            trace_id=trace_id,
        )
        session.add(
            TicketEvent(
                id=uuid4(),
                tenant_id=ticket.tenant_id,
                ticket_id=ticket.id,
                seq=record.seq,
                event_type=record.event_type,
                from_state=record.from_state,
                to_state=record.to_state,
                actor_id=actor_id,
                payload=dict(record.payload),
                trace_id=trace_id,
                prev_hash=record.prev_hash,
                hash=record.hash,
            )
        )
        # 当日锚点与事件同事务推进：热路径 O(1) 比对链尾用（见 audit_guard）
        await session.execute(
            text(
                "INSERT INTO erp.ticket_event_anchor (ticket_id, seq, hash) "
                "VALUES (:ticket_id, :seq, :hash) "
                "ON CONFLICT (ticket_id) DO UPDATE SET seq = EXCLUDED.seq, "
                "hash = EXCLUDED.hash, updated_at = now()"
            ),
            {"ticket_id": ticket.id, "seq": record.seq, "hash": record.hash},
        )
        return record

    async def record_recovery(
        self,
        *,
        ticket_id: UUID,
        step_no: int,
        idem_key: str,
        verify_observed: Mapping[str, Any] | None,
        prior_recoveries: int,
        trace_id: str | None = None,
    ) -> EventRecord:
        """确认「本地未提交但外部已成功」时写显式事件，带可追溯的证据坐标。"""

        async with self._sf() as session, session.begin():
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFound(f"工单不存在: {ticket_id}")
            return await self._append_locked(
                session,
                ticket,
                event_type=RECOVERED_EVENT,
                from_state=None,
                to_state=None,
                payload={
                    "step_no": step_no,
                    "idem_key": idem_key,
                    "verify_observed": dict(verify_observed or {}),
                    "prior_recoveries": prior_recoveries,
                    "evidence": {
                        "step_no": step_no,
                        "idem_key": idem_key,
                        "verify": dict(verify_observed or {}),
                    },
                },
                actor_id=None,
                trace_id=trace_id,
            )

    async def count_recoveries(
        self,
        ticket_id: UUID,
        *,
        step_no: int | None = None,
        today_only: bool = False,
    ) -> int:
        """recovered 指标：按工单（可选按步骤 / 当日）计数，供阈值判断。"""

        sql = (
            "SELECT count(*) FROM erp.ticket_event "
            "WHERE ticket_id = :ticket_id AND event_type = :event_type"
        )
        params: dict[str, Any] = {
            "ticket_id": ticket_id,
            "event_type": RECOVERED_EVENT,
        }
        if step_no is not None:
            sql += " AND payload->>'step_no' = :step_no"
            params["step_no"] = str(step_no)
        if today_only:
            sql += " AND created_at >= date_trunc('day', now())"
        async with self._sf() as session:
            return int((await session.execute(text(sql), params)).scalar_one())

    async def record_compensation(
        self,
        *,
        ticket_id: UUID,
        compensate_seq: int,
        state: str,
        trace_id: str | None = None,
    ) -> None:
        """补偿进度只进事件表用于审计，不改变工单状态投影。"""

        async with self._sf() as session, session.begin():
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFound(f"工单不存在: {ticket_id}")
            await self._append_locked(
                session,
                ticket,
                event_type=COMPENSATION_EVENT,
                from_state=None,
                to_state=None,
                payload={"compensate_seq": compensate_seq, "state": state},
                actor_id=None,
                trace_id=trace_id,
            )


def step_states(steps: Sequence[TicketStep]) -> tuple[str, ...]:
    return tuple(step.state for step in steps)
