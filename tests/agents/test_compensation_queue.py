"""补偿队列：结构约束 + ``compensate_seq`` 倒序 + 独立执行。"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from apps.platform.contracts.ticket import TicketState

from apps.agents.runtime import PlannedStep
from apps.agents.state_machine import TransitionContext

from dbutil import db_env, prepare_executing
from support import SCENE, ScriptedConnector, build_runtime, make_action

PAYLOAD = {"supplier": "SUP-1", "amount": "10.00"}


async def _succeeded_step(env, ticket_id, step_no: int, action_key: str | None = None):
    action = action_key or f"fake.draft.create.{step_no}"
    step = await env.store.intent_step(
        ticket_id=ticket_id,
        step_no=step_no,
        action_key=action,
        target_system="fake",
        write_level=1,
        idem_key=f"idem-{step_no}",
        request_hash=hashlib.sha256(repr(PAYLOAD).encode()).hexdigest(),
        request_snapshot=PAYLOAD,
    )
    return await env.store.mark_step(step_id=step.id, state="succeeded")


def _step(key: str, idem: str) -> PlannedStep:
    return PlannedStep(
        action=make_action(key),
        target_system="fake",
        payload=PAYLOAD,
        idem_key=idem,
    )


def test_enqueue_is_idempotent_and_orders_desc(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            for step_no in (1, 2, 3):
                await _succeeded_step(env, ticket.id, step_no)

            tasks = await env.queue.enqueue(ticket.id)
            assert [task.compensate_seq for task in tasks] == [3, 2, 1]

            again = await env.queue.enqueue(ticket.id)
            assert [task.compensate_seq for task in again] == [3, 2, 1]
            assert (await env.queue.counts(ticket.id)).queued == 3

    asyncio.run(scenario())


def test_compensate_seq_must_equal_original_step_no(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            step = await _succeeded_step(env, ticket.id, 1)

            async with env.sf() as session:
                with pytest.raises(DBAPIError) as excinfo:
                    await session.execute(
                        text(
                            "INSERT INTO erp.compensation_task "
                            "(id, ticket_id, original_step, compensate_seq, action_key, state) "
                            "VALUES (:id, :ticket, :step, 99, 'fake.x', 'queued')"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "ticket": ticket.id,
                            "step": step.id,
                        },
                    )
                assert "compensate_seq" in str(excinfo.value)

    asyncio.run(scenario())


def test_only_one_running_task_per_ticket(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            for step_no in (1, 2):
                await _succeeded_step(env, ticket.id, step_no)
            await env.queue.enqueue(ticket.id)

            first = await env.queue.claim_next(ticket.id)
            assert first is not None and first.compensate_seq == 2

            # 队列里再出现第二条 running，必须被唯一索引拒绝（不是靠代码自觉）
            async with env.sf() as session:
                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            "UPDATE erp.compensation_task SET state = 'running' "
                            "WHERE ticket_id = :ticket AND state = 'queued'"
                        ),
                        {"ticket": ticket.id},
                    )

    asyncio.run(scenario())


def test_claim_order_is_desc_on_real_queue(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            for step_no in (1, 2, 3):
                await _succeeded_step(env, ticket.id, step_no)
            await env.queue.enqueue(ticket.id)

            claimed = []
            while True:
                task = await env.queue.claim_next(ticket.id)
                if task is None:
                    break
                claimed.append(task.compensate_seq)
                await env.queue.mark_done(task.id)
            assert claimed == [3, 2, 1]

    asyncio.run(scenario())


def test_saga_compensates_in_reverse_and_rolls_back(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector(
                behaviours={"fake.draft.create.3": "unknown_absent"}
            )
            runtime = build_runtime(env, connector, max_attempts=1)

            steps = [
                _step("fake.draft.create.1", "idem-1"),
                _step("fake.draft.create.2", "idem-2"),
                _step("fake.draft.create.3", "idem-3"),
            ]
            report = await runtime.execute(
                ticket.id, owner="worker-a", steps=steps, scene=SCENE
            )
            assert report.status is TicketState.COMPENSATING

            tasks = await env.queue.list_tasks(ticket.id)
            assert [task.compensate_seq for task in tasks] == [2, 1]

            compensated = await runtime.compensate(ticket.id, owner="worker-a")
            assert compensated.status is TicketState.ROLLED_BACK
            assert compensated.done == 2
            assert connector.compensations == ["idem-2:undo", "idem-1:undo"]
            assert (await env.store.get_ticket(ticket.id)).state == TicketState.ROLLED_BACK.value

    asyncio.run(scenario())


def test_compensation_failure_requires_manual_handling(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            for step_no in (1, 2):
                await _succeeded_step(env, ticket.id, step_no)
            await env.store.transition(
                ticket.id,
                "补偿中",
                "step_failed",
                context=TransitionContext(fail_code="F6", step_states=("succeeded", "failed")),
            )
            connector = ScriptedConnector(compensate_failures=100)
            runtime = build_runtime(env, connector)

            report = await runtime.compensate(ticket.id, owner="worker-a")
            assert report.status is TicketState.MANUAL_REQUIRED
            assert report.failed >= 1
            counts = await env.queue.counts(ticket.id)
            assert counts.failed == 2
            assert (await env.store.get_ticket(ticket.id)).state == TicketState.MANUAL_REQUIRED.value

    asyncio.run(scenario())
