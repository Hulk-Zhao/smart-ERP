"""工单事件真源与投影的持久化（SQLAlchemy 2.0 async / PG15）。

约定：
- 只有本模块可以写 ``ticket`` / ``ticket_event`` / ``ticket_step``；
- 每次事件追加都先对 ``ticket`` 行加锁，``seq`` 与哈希链因此天然串行，不会分叉；
- ``ticket`` 的 ``state`` / ``version`` 是投影，任何时刻可由 :meth:`rebuild_projection` 复算。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.platform.contracts.ticket import TicketState
from apps.platform.db.models import Ticket, TicketEvent, TicketStep

from apps.agents.events import (
    COMPENSATION_EVENT,
    CREATED_EVENT,
    STEP_EVENT,
    TRANSITION_EVENT,
    EventRecord,
    Projection,
    make_event,
    replay,
)
from apps.agents.state_machine import TransitionContext, resolve_transition


class StoreError(Exception):
    """持久化层拒绝操作。"""


class TicketNotFound(StoreError):
    pass


class DuplicateTicket(StoreError):
    """``UNIQUE(tenant_id, scene_code, business_key)`` 命中（FR-1101 幂等）。"""


class DuplicateStep(StoreError):
    """同一工单内 step_no 重复（断点续跑不得重放已存在的步骤）。"""


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
    ) -> Ticket:
        ticket = Ticket(
            id=uuid4(),
            tenant_id=tenant_id,
            ticket_no=ticket_no,
            scene_code=scene_code,
            initiator_id=initiator_id,
            trigger_kind=trigger_kind,
            business_key=business_key,
            state=TicketState.PLANNED.value,
            version=0,
            diff_amount=diff_amount,
            systems_count=systems_count,
            touches_fund=touches_fund,
        )
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
                    payload={"scene_code": scene_code, "business_key": business_key},
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
    ) -> EventRecord:
        """校验 + 追加事件 + 更新投影，同一事务内完成。"""

        async with self._sf() as session, session.begin():
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFound(f"工单不存在: {ticket_id}")

            target_state = TicketState(target)
            resolve_transition(ticket.state, target_state, trigger, context)

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
        return record

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
        return record

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
