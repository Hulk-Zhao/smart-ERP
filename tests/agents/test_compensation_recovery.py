"""补偿崩溃恢复：stale running 回收、断点续跑、严格倒序、attempt 上限转人工。"""

from __future__ import annotations

import asyncio

import pytest
from dbutil import db_env, prepare_executing
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from support import SCENE, ScriptedConnector, build_runtime, make_action

from apps.agents.alerts import RecordingAlertSink
from apps.agents.runtime import PlannedStep
from apps.platform.contracts.ticket import TicketState


class WorkerCrash(BaseException):
    """模拟进程被杀：不会被 except Exception 兜住。"""


def _step(key: str) -> PlannedStep:
    return PlannedStep(
        action=make_action(key),
        target_system="fake",
        payload={"supplier": "S-001", "amount": "100.00"},
        idem_key=f"idem-{key}",
    )


async def _set_attempts(url: str, ticket_id, compensate_seq: int, attempts: int) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE erp.compensation_task SET attempts = :attempts "
                    "WHERE ticket_id = :ticket_id AND compensate_seq = :seq"
                ),
                {"attempts": attempts, "ticket_id": ticket_id, "seq": compensate_seq},
            )
    finally:
        await engine.dispose()


async def _assert_resume_case(db: str) -> None:
    async with db_env(db) as env:
        ticket = await prepare_executing(env.store)
        steps = [_step(f"s{i}") for i in range(1, 5)]
        connector = ScriptedConnector(behaviours={"s4": "transient"})
        runtime = build_runtime(env, connector, max_attempts=1)
        report = await runtime.execute(
            ticket.id, owner="exec", steps=steps, scene=SCENE
        )
        assert report.status is TicketState.COMPENSATING

        tasks = await env.queue.list_tasks(ticket.id)
        assert [task.compensate_seq for task in tasks] == [3, 2, 1]

        crashed = {"done": False}
        invoked: list[str] = []

        async def crashing_compensator(call, idem_key):
            invoked.append(call.original_idem_key)
            if call.original_idem_key == "idem-s2" and not crashed["done"]:
                crashed["done"] = True
                raise WorkerCrash("第 2 步补偿时进程死亡")
            return await connector.compensate(call, idem_key=idem_key)

        alerts = RecordingAlertSink()
        first = build_runtime(
            env,
            connector,
            compensator=crashing_compensator,
            compensation_stale_seconds=0.0,
            alert_sink=alerts,
        )
        with pytest.raises(WorkerCrash):
            await first.compensate(ticket.id, owner="comp-1")

        mid = {
            task.compensate_seq: task.state
            for task in await env.queue.list_tasks(ticket.id)
        }
        assert mid == {3: "done", 2: "running", 1: "queued"}
        assert (await env.store.get_ticket(ticket.id)).state == TicketState.COMPENSATING.value

        # 重启：先走启动恢复入口回收 stale running，再继续补偿
        second = build_runtime(
            env,
            connector,
            compensation_stale_seconds=0.0,
            alert_sink=alerts,
        )
        reclaimed = await second.recover_stale_compensations()
        assert [task.compensate_seq for task in reclaimed] == [2]
        assert "COMPENSATION_STALE_RECLAIMED" in alerts.codes()

        resumed = await second.compensate(ticket.id, owner="comp-2")

        assert resumed.status is TicketState.ROLLED_BACK
        assert resumed.order == (3, 2, 1)
        # 外部撤销严格 3→2→1 各一次；崩溃的 s2 死在外部调用前，恢复后才真正撤销
        assert connector.compensations == [
            "idem-s3:undo",
            "idem-s2:undo",
            "idem-s1:undo",
        ]
        assert invoked == ["idem-s3", "idem-s2"]
        final = {
            task.compensate_seq: task.state
            for task in await env.queue.list_tasks(ticket.id)
        }
        assert final == {3: "done", 2: "done", 1: "done"}
        assert (await env.store.get_ticket(ticket.id)).state == TicketState.ROLLED_BACK.value


async def _assert_attempt_limit_case(db: str) -> None:
    async with db_env(db) as env:
        ticket = await prepare_executing(env.store)
        steps = [_step(f"s{i}") for i in range(1, 5)]
        connector = ScriptedConnector(behaviours={"s4": "transient"})
        runtime = build_runtime(env, connector, max_attempts=1)
        await runtime.execute(ticket.id, owner="exec", steps=steps, scene=SCENE)

        # 模拟同一步骤反复崩溃：把 seq=2 的尝试次数打到上限
        await _set_attempts(db, ticket.id, 2, 3)

        alerts = RecordingAlertSink()
        compensator = build_runtime(
            env,
            connector,
            compensation_stale_seconds=0.0,
            alert_sink=alerts,
        )
        report = await compensator.compensate(ticket.id, owner="comp-1")

        assert report.status is TicketState.MANUAL_REQUIRED
        assert "COMPENSATION_ATTEMPTS_EXCEEDED" in alerts.codes()
        final = {
            task.compensate_seq: task.state
            for task in await env.queue.list_tasks(ticket.id)
        }
        assert final[2] == "failed"
        assert (await env.store.get_ticket(ticket.id)).state == TicketState.MANUAL_REQUIRED.value


def test_compensation_resumes_from_step_2_and_keeps_desc_order(db: str) -> None:
    asyncio.run(_assert_resume_case(db))


def test_compensation_attempt_limit_escalates_to_manual(db: str) -> None:
    asyncio.run(_assert_attempt_limit_case(db))
