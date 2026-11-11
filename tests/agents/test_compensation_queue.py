"""补偿队列：结构约束 + ``compensate_seq`` 倒序 + 独立执行。"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from unittest import mock

import pytest
from dbutil import db_env, prepare_executing
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from support import SCENE, ScriptedConnector, build_runtime, make_action

import apps.agents.runtime as runtime_module
from apps.agents.runtime import PlannedStep
from apps.agents.state_machine import TransitionContext
from apps.platform.contracts.ticket import TicketState

PAYLOAD = {"supplier": "SUP-1", "amount": "10.00"}


async def _noop_sleep(_seconds: float) -> None:
    """不睡觉：退避由 ``next_retry_at`` 表达，测试只验证「到期后能继续」。"""


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


def test_zero_delay_retry_must_not_depend_on_clock(db: str) -> None:
    """``retry_in_seconds=0`` 必须落成 NULL，不能写成 ``now()``。

    PG 的 ``now()`` 不保证单调：Docker Desktop / Hyper-V 的时间同步会把容器时钟
    回拨约 0.8–1.4 秒（本地 60 秒内采样到 4 次）。只要回拨发生在 ``mark_failed``
    与下一次 ``claim_next`` 之间，``next_retry_at <= now()`` 就为假：
    claim 返回 None → 补偿循环提前 break → ``failed`` 少一条、``queued`` 多一条，
    也就是 ``test_compensation_failure_requires_manual_handling`` 偶发的
    ``failed 1 != 2``。零延迟重试应写 NULL（与 ``reclaim_stale`` 同义），
    让「可领取」不依赖服务器时钟。
    """

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            await _succeeded_step(env, ticket.id, 1)
            await env.queue.enqueue(ticket.id)
            task = await env.queue.claim_next(ticket.id)
            assert task is not None

            await env.queue.mark_failed(task.id, "boom", retry_in_seconds=0.0)
            async with env.sf() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT state, next_retry_at FROM erp.compensation_task "
                            "WHERE ticket_id = :ticket"
                        ),
                        {"ticket": ticket.id},
                    )
                ).mappings().one()
            assert row["state"] == "queued"
            assert row["next_retry_at"] is None, (
                "retry_in_seconds=0 被写成了 now()：服务器时钟回拨时该任务会暂时不可领取，"
                "补偿循环会提前结束"
            )
            assert await env.queue.claim_next(ticket.id) is not None

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


def test_compensation_round_distinguishes_backoff_from_empty(db: str) -> None:
    """``claim_next() is None`` 的两种含义必须分开留痕、也必须分开决定状态。

    旧实现在两种情况下都静默 ``break``，随后 ``counts.queued > 0`` 就转
    MANUAL_REQUIRED；而 ``compensate`` 要求工单仍在「补偿中」，于是退避到期后
    没有任何一轮会再来处理它。现在：仍在退避 → 工单保持「补偿中」并留
    ``reason=backoff``（带未处理条数）；队列真空 → ``reason=empty``。
    """

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
            await env.queue.enqueue(ticket.id)

            # 把 seq=2 推进 60s 退避，本轮只剩 seq=1 可领取
            gated = await env.queue.claim_next(ticket.id)
            assert gated is not None and gated.compensate_seq == 2
            await env.queue.mark_failed(gated.id, "boom", retry_in_seconds=60.0)
            attempts_after_fail = gated.attempts

            runtime = build_runtime(env, ScriptedConnector())
            with mock.patch.object(runtime_module.logger, "warning") as warning:
                report = await runtime.compensate(ticket.id, owner="worker-a")

            messages = [str(call.args[0] % call.args[1:]) for call in warning.call_args_list]
            assert any("reason=backoff" in m and "未处理=1" in m for m in messages), messages
            assert report.status is TicketState.COMPENSATING
            assert (await env.store.get_ticket(ticket.id)).state == TicketState.COMPENSATING.value

            task = next(
                t for t in await env.queue.list_tasks(ticket.id) if t.id == gated.id
            )
            assert task.state == "queued"
            assert task.attempts == attempts_after_fail, "本轮没执行到它，attempts 不应上涨"

    asyncio.run(scenario())


def test_compensation_retry_survives_backoff_and_finishes(db: str) -> None:
    """第一次补偿失败（retry_in>0）不得判人工；退避到期后第二轮要能做完。

    这是「补偿重试真的在工作」的唯一证据：旧实现里第一次可重试失败就会因
    ``claim_next() is None`` → ``counts.queued > 0`` 转 MANUAL_REQUIRED，
    重试永远没有下一轮，之后只会每秒抛 ``RuntimeGuardError``。
    """

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

            # 只失败一次：seq=2 的第一次撤销失败，别的都成功
            connector = ScriptedConnector(compensate_failures=1)
            runtime = build_runtime(
                env,
                connector,
                compensation_retry_seconds=8.0,
                sleep=_noop_sleep,
            )

            first = await runtime.compensate(ticket.id, owner="comp-1")

            assert first.status is TicketState.COMPENSATING
            assert (await env.store.get_ticket(ticket.id)).state == TicketState.COMPENSATING.value, (
                "任务还在退避就转人工，重试路径永远走不到"
            )
            gated = next(
                t for t in await env.queue.list_tasks(ticket.id) if t.compensate_seq == 2
            )
            assert gated.state == "queued"
            assert gated.attempts == 1, "第一次失败后 attempts 应已 +1"
            assert first.failed == 0, "报告数字只能来自库：没有终态失败就不能写 1"

            # 让退避到期：不睡觉，直接把闸门打开（与生产「时间到了」等价）
            async with env.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.compensation_task SET next_retry_at = NULL "
                        "WHERE ticket_id = :ticket"
                    ),
                    {"ticket": ticket.id},
                )

            second = await runtime.compensate(ticket.id, owner="comp-1")

            assert second.status is TicketState.ROLLED_BACK
            assert (await env.store.get_ticket(ticket.id)).state == TicketState.ROLLED_BACK.value
            final = {
                t.compensate_seq: t.state for t in await env.queue.list_tasks(ticket.id)
            }
            assert final == {1: "done", 2: "done"}
            assert second.done == 2 and second.failed == 0

    asyncio.run(scenario())


def test_compensation_round_logs_empty_queue(db: str) -> None:
    """全部处理完时留 ``reason=empty``，与「退避跳过」区分开。"""

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
            runtime = build_runtime(env, ScriptedConnector())
            with mock.patch.object(runtime_module.logger, "info") as info:
                report = await runtime.compensate(ticket.id, owner="worker-a")

            messages = [str(call.args[0] % call.args[1:]) for call in info.call_args_list]
            assert any("reason=empty" in m and "processed=2" in m for m in messages), messages
            assert report.status is TicketState.ROLLED_BACK

    asyncio.run(scenario())
