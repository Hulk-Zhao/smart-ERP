"""执行计划持久化：重启后不带 steps 也能自行续跑。

T305 的续跑只在调用方**重新给一遍 steps** 时成立；``ticket_step`` 只存
``request_snapshot``（执行到哪一步的输入），没有「还剩哪些步骤」这份计划。
本用例按缺口卡 B-RESUME-NO-PLAN-STORE 的验收方式来：

1. 多步工单在第二步写入时进程被杀（``BaseException``，运行时的失败分类接不住）；
2. 新进程只用恢复入口 ``resume()``，**不传 steps**；
3. 断言能自行续跑到「校验中」→ 完成，且库里能读到持久化计划。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from dbutil import db_env, prepare_executing
from support import SCENE, ScriptedConnector, build_runtime, make_action

from apps.agents.runtime import PlannedStep, RuntimeGuardError
from apps.agents.store import StoreError
from apps.platform.connectors.sdk import VerifyResult
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.registry import ActionRegistry

STEP_ONE = "fake.draft.one"
STEP_TWO = "fake.draft.two"
PAYLOAD_ONE = {"supplier": "SUP-1", "amount": "10.00"}
PAYLOAD_TWO = {"supplier": "SUP-1", "amount": "20.00"}


class WorkerKilled(BaseException):
    """模拟进程被杀：不是 ``Exception``，运行时不得把它当成业务失败接住。"""


class KillingConnector(ScriptedConnector):
    """第一步正常写入，第二步写入时进程死亡。"""

    async def write(self, action, payload, *, ticket_id: str, idem_key: str):  # type: ignore[override]
        if action.key == STEP_TWO:
            raise WorkerKilled("worker 在第二步写入时被杀")
        return await super().write(action, payload, ticket_id=ticket_id, idem_key=idem_key)


class AbsentConnector(ScriptedConnector):
    """续跑时第一次 verify 回答「没写成」，重放写入后按正常逻辑判定。"""

    _absent_probe: bool = True

    async def verify(self, spec, payload):  # type: ignore[override]
        if self._absent_probe:
            self._absent_probe = False
            self.verify_calls += 1
            self.calls.append(("verify", spec.kind))
            return VerifyResult(status="absent")
        return await super().verify(spec, payload)


def _steps() -> list[PlannedStep]:
    return [
        PlannedStep(
            action=make_action(STEP_ONE),
            target_system="fake",
            payload=PAYLOAD_ONE,
            idem_key="idem-1",
        ),
        PlannedStep(
            action=make_action(STEP_TWO),
            target_system="fake",
            payload=PAYLOAD_TWO,
            idem_key="idem-2",
        ),
    ]


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register(make_action(STEP_ONE))
    registry.register(make_action(STEP_TWO))
    return registry


def test_resume_without_steps_reads_plan_and_reaches_verifying(db: str) -> None:
    """杀 worker 后，不带 steps 的恢复入口必须能自行续跑到「校验中」。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            killed = KillingConnector()
            runtime = build_runtime(env, killed, actions=_registry())

            with pytest.raises(WorkerKilled):
                await runtime.execute(ticket.id, owner="worker-1", steps=_steps(), scene=SCENE)

            # 1) 计划本体在库里，不是只有 ticket_step.request_snapshot
            async with env.sf() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT step_no, action_key, target_system, payload, idem_key "
                            "FROM erp.ticket_plan_step WHERE ticket_id = :id ORDER BY step_no"
                        ),
                        {"id": ticket.id},
                    )
                ).mappings().all()
            assert [row["step_no"] for row in rows] == [1, 2]
            assert [row["action_key"] for row in rows] == [STEP_ONE, STEP_TWO]
            assert [dict(row["payload"]) for row in rows] == [PAYLOAD_ONE, PAYLOAD_TWO]
            assert [row["idem_key"] for row in rows] == ["idem-1", "idem-2"]
            assert {row["target_system"] for row in rows} == {"fake"}

            # 工单仍停在「执行中」，第二步只落了 intent
            before = await env.store.get_ticket(ticket.id)
            assert before.state == TicketState.EXECUTING.value
            assert [step.state for step in await env.store.list_steps(ticket.id)] == ["succeeded", "intent"]

            # 2) 新进程：只给恢复入口和动作目录，不给 steps
            fresh_connector = AbsentConnector()
            fresh = build_runtime(env, fresh_connector, actions=_registry())
            report = await fresh.resume(ticket.id, owner="worker-2", scene=SCENE)

            # 3) 自行续跑到校验中并收口完成；第一步不重放，第二步确认未写成后重放
            assert report.status is TicketState.COMPLETED
            assert [item.state for item in report.outcomes] == ["succeeded", "succeeded"]
            assert [write[0] for write in killed.writes] == [STEP_ONE]
            assert [write[0] for write in fresh_connector.writes] == [STEP_TWO]
            events = await env.store.list_events(ticket.id)
            assert any(event.to_state == TicketState.VERIFYING.value for event in events)
            after = await env.store.get_ticket(ticket.id)
            assert after.state == TicketState.COMPLETED.value

    asyncio.run(scenario())


def test_resume_without_persisted_plan_fails_explicitly(db: str) -> None:
    """没有计划就必须报错，不能假装续跑成功。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            runtime = build_runtime(env, ScriptedConnector(), actions=_registry())
            with pytest.raises(RuntimeGuardError) as err:
                await runtime.resume(ticket.id, owner="worker-2", scene=SCENE)
            assert "没有持久化执行计划" in str(err.value)

    asyncio.run(scenario())


def test_execute_with_a_different_plan_is_rejected(db: str) -> None:
    """同一工单已落计划后再给另一份步骤，必须拒绝，避免执行与计划背离。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            runtime = build_runtime(env, KillingConnector(), actions=_registry())
            with pytest.raises(WorkerKilled):
                await runtime.execute(ticket.id, owner="worker-1", steps=_steps(), scene=SCENE)

            other = [
                PlannedStep(
                    action=make_action(STEP_ONE),
                    target_system="fake",
                    payload=PAYLOAD_ONE,
                    idem_key="idem-other",
                )
            ]
            with pytest.raises(StoreError):
                await runtime.execute(ticket.id, owner="worker-1", steps=other, scene=SCENE)

    asyncio.run(scenario())
