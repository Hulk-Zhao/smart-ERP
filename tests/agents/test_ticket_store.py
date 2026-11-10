"""事件真源 ↔ 投影一致性（真实 PG15）。"""

from __future__ import annotations

import asyncio
import hashlib
from itertools import pairwise

import pytest
from dbutil import db_env, new_ticket
from sqlalchemy import text

from apps.agents.events import ReplayError, replay
from apps.agents.state_machine import IllegalTransitionError, TransitionContext
from apps.agents.store import DuplicateTicket
from apps.platform.contracts.ticket import TicketState


def _intent_kwargs(ticket_id, step_no: int = 1) -> dict[str, object]:
    payload = {"supplier": "SUP-1", "amount": "10.00"}
    return {
        "ticket_id": ticket_id,
        "step_no": step_no,
        "action_key": f"fake.draft.create.{step_no}",
        "target_system": "fake",
        "write_level": 1,
        "idem_key": f"idem-{step_no}",
        "request_hash": hashlib.sha256(repr(payload).encode()).hexdigest(),
        "request_snapshot": payload,
    }


def test_projection_rebuild_equals_stored_state(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store)
            await env.store.transition(
                ticket.id,
                "待审批",
                "preview_ok",
                context=TransitionContext(preview_ok=True),
            )
            await env.store.transition(
                ticket.id,
                "执行中",
                "approved",
                context=TransitionContext(approved=True),
            )
            step = await env.store.intent_step(**_intent_kwargs(ticket.id))  # type: ignore[arg-type]
            await env.store.mark_step(step_id=step.id, state="succeeded")
            await env.store.transition(
                ticket.id,
                "校验中",
                "all_ok",
                context=TransitionContext(step_states=("succeeded",)),
            )
            await env.store.transition(
                ticket.id,
                "已完成",
                "verified",
                context=TransitionContext(verified=True, step_states=("succeeded",)),
            )

            check = await env.store.check_projection(ticket.id)
            assert check.consistent
            assert check.stored_state is TicketState.COMPLETED

            events = await env.store.list_events(ticket.id)
            assert [event.seq for event in events] == list(range(1, len(events) + 1))
            assert events[0].event_type == "created"
            for previous, current in pairwise(events):
                assert current.prev_hash == previous.hash
            assert replay(events).state is TicketState.COMPLETED

            projection = await env.store.rebuild_projection(ticket.id)
            assert projection.version == 4
            assert projection.steps == {1: "succeeded"}

    asyncio.run(scenario())


def test_illegal_transition_leaves_no_event(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store)
            before = len(await env.store.list_events(ticket.id))
            with pytest.raises(IllegalTransitionError) as excinfo:
                await env.store.transition(ticket.id, "已完成", "verified")
            assert excinfo.value.code.value == "E-STATE-ILLEGAL"
            assert len(await env.store.list_events(ticket.id)) == before
            stored = await env.store.get_ticket(ticket.id)
            assert stored.state == TicketState.PLANNED.value
            assert stored.version == 0

    asyncio.run(scenario())


def test_repair_projection_from_events(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store)
            await env.store.transition(
                ticket.id,
                "待审批",
                "preview_ok",
                context=TransitionContext(preview_ok=True),
            )
            # 模拟投影被写坏（真源不变）
            async with env.sf() as session, session.begin():
                await session.execute(
                    text("UPDATE erp.ticket SET state = '已完成' WHERE id = :id"),
                    {"id": ticket.id},
                )
            broken = await env.store.check_projection(ticket.id)
            assert broken.consistent is False
            assert broken.stored_state is TicketState.COMPLETED
            assert broken.replay_state is TicketState.PENDING_APPROVAL

            repaired = await env.store.repair_projection(ticket.id)
            assert repaired.replay_state is TicketState.PENDING_APPROVAL
            assert (await env.store.check_projection(ticket.id)).consistent

    asyncio.run(scenario())


def test_duplicate_business_key_is_rejected(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store, business_key="2026-10/SUP-9")
            with pytest.raises(DuplicateTicket):
                await new_ticket(env.store, business_key="2026-10/SUP-9")
            assert ticket.business_key == "2026-10/SUP-9"

    asyncio.run(scenario())


def test_tampered_event_breaks_replay(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store)
            await env.store.transition(
                ticket.id,
                "待审批",
                "preview_ok",
                context=TransitionContext(preview_ok=True),
            )
            events = await env.store.list_events(ticket.id)
            # 触发器禁止改历史事件；这里只验证纯函数能识别被改过的内容
            tampered = list(events)
            last = tampered[-1]
            tampered[-1] = type(last)(
                seq=last.seq,
                event_type=last.event_type,
                from_state=last.from_state,
                to_state=last.to_state,
                payload={**last.payload, "trigger": "forged"},
                actor_id=last.actor_id,
                trace_id=last.trace_id,
                prev_hash=last.prev_hash,
                hash=last.hash,
            )
            with pytest.raises(ReplayError):
                replay(tampered)

    asyncio.run(scenario())
