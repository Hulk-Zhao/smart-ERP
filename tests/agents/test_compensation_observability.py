"""补偿观测写入点：overdue（含 NULL 分支）、future_retry、采集失败不拖崩主流程。

口径与指标名来自 ``observability/metrics.py``（S7），这里只验证 apps 侧写入。
"""

from __future__ import annotations

import asyncio

from dbutil import db_env, new_ticket, prepare_executing
from sqlalchemy import text
from support import SCENE, ScriptedConnector, build_runtime, make_action

from apps.agents.runtime import PlannedStep
from apps.agents.state_machine import TransitionContext
from apps.platform.contracts.ticket import TicketState
from observability.metrics import MetricsRegistry

PAYLOAD = {"supplier": "SUP-1", "amount": "10.00"}


def _step(key: str) -> PlannedStep:
    return PlannedStep(
        action=make_action(key),
        target_system="fake",
        payload=PAYLOAD,
        idem_key=f"idem-{key}",
    )


async def _compensating_with_one_task(env, registry: MetricsRegistry):
    """s1 成功、s2 瞬时失败 → 工单补偿中，队列里恰好 1 条 queued 补偿任务。"""

    ticket = await prepare_executing(env.store)
    connector = ScriptedConnector(behaviours={"s2": "transient"})
    runtime = build_runtime(env, connector, max_attempts=1, metrics=registry)
    report = await runtime.execute(
        ticket.id, owner="exec", steps=[_step("s1"), _step("s2")], scene=SCENE
    )
    assert report.status is TicketState.COMPENSATING
    return ticket, runtime


async def _patch_task(env, ticket_id, assignment: str) -> None:
    async with env.sf() as session, session.begin():
        await session.execute(
            text(
                "UPDATE erp.compensation_task SET " + assignment
                + " WHERE ticket_id = :ticket_id AND state = 'queued'"
            ),
            {"ticket_id": ticket_id},
        )


def test_future_retry_is_counted(db: str) -> None:
    """① next_retry_at 在未来且超过 18s → future_retry 计入。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            registry = MetricsRegistry()
            ticket, runtime = await _compensating_with_one_task(env, registry)
            await _patch_task(env, ticket.id, "next_retry_at = now() + interval '30 seconds'")

            await runtime.compensate(ticket.id, owner="cmp")

            assert registry.compensation_future_retry.value() == 1.0
            assert registry.compensation_overdue.value() == 0.0

    asyncio.run(scenario())


def test_overdue_null_retry_branch_is_counted(db: str) -> None:
    """② next_retry_at 为 NULL 且 updated_at 超过 60s → overdue 计入（防漏 NULL 分支）。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            registry = MetricsRegistry()
            ticket, runtime = await _compensating_with_one_task(env, registry)
            await _patch_task(
                env,
                ticket.id,
                "next_retry_at = NULL, updated_at = now() - interval '61 seconds'",
            )

            await runtime.compensate(ticket.id, owner="cmp")

            assert registry.compensation_overdue.value() == 1.0
            assert registry.compensation_future_retry.value() == 0.0

    asyncio.run(scenario())


def test_metrics_sql_failure_does_not_break_round(db: str) -> None:
    """③ 统计 SQL 抛错时补偿循环仍完成本轮，且不写假 0。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            registry = MetricsRegistry()
            ticket, runtime = await _compensating_with_one_task(env, registry)

            async def boom(**_kwargs):
                raise RuntimeError("metrics sql 挂了")

            env.queue.metrics_counts = boom  # type: ignore[method-assign]

            report = await runtime.compensate(ticket.id, owner="cmp")

            assert report.status is TicketState.ROLLED_BACK
            # 缺样本而不是假 0：两个 gauge 都保持未写入
            assert registry.compensation_overdue.value() is None
            assert registry.compensation_future_retry.value() is None

    asyncio.run(scenario())


async def _ticket_reaching(env, *hops) -> None:
    """建单后按 (目标态, 触发名, 上下文) 逐跳推进，返回最终落点由调用方断言。"""

    ticket = await new_ticket(env.store)
    for target, trigger, context in hops:
        await env.store.transition(ticket.id, target, trigger, context=context)


def test_unresolved_tickets_follow_state_contract(db: str) -> None:
    """③ 状态流转后 unresolved 数值符合口径。

    已解决只含：已完成（业务闭合）、已回滚（补偿撤销）、已终止/已驳回（零写入或策略拒绝）。
    需人工处置（失败没人接住）与已锁定（外部已生效待对账）计入未解决；
    已完成与已回滚分别列出，不合成一个 ``closed``。
    """

    async def scenario() -> None:
        async with db_env(db) as env:
            approved = (
                ("待审批", "preview_ok", TransitionContext(preview_ok=True)),
                ("执行中", "approved", TransitionContext(approved=True)),
            )
            await _ticket_reaching(env)  # 计划中 → 未解决
            await _ticket_reaching(
                env,
                *approved,
                ("校验中", "all_ok", TransitionContext(step_states=("succeeded",))),
                (
                    "已完成",
                    "verified",
                    TransitionContext(verified=True, step_states=("succeeded",)),
                ),
            )  # 已完成 → 已解决
            await _ticket_reaching(
                env,
                *approved,
                ("补偿中", "step_failed", TransitionContext(fail_code="F5")),
                ("已回滚", "all_compensated", TransitionContext(compensation_failed=0)),
            )  # 已回滚 → 已解决
            await _ticket_reaching(
                env,
                *approved,
                ("补偿中", "step_failed", TransitionContext(fail_code="F5")),
                (
                    "需人工处置",
                    "compensation_failed",
                    TransitionContext(compensation_failed=1),
                ),
            )  # 需人工处置 → 未解决
            await _ticket_reaching(
                env, ("已终止", "preview_failed", TransitionContext(writes=0))
            )  # 已终止 → 已解决
            await _ticket_reaching(
                env,
                ("待审批", "preview_ok", TransitionContext(preview_ok=True)),
                ("已驳回", "rejected", TransitionContext()),
            )  # 已驳回 → 已解决
            await _ticket_reaching(
                env,
                *approved,
                ("已锁定", "external_effective", TransitionContext(fail_code="F8")),
            )  # 已锁定 → 未解决

            # 未解决 = 计划中 + 需人工处置 + 已锁定
            assert await env.store.count_unresolved() == 3

            registry = MetricsRegistry()
            runtime = build_runtime(env, ScriptedConnector(), metrics=registry)
            # 周期写入点挂在补偿轮次入口；公开入口 compensate() 的覆盖见前两条用例
            await runtime._observe_compensation()

            assert registry.unresolved_tickets.value() == 3.0

    asyncio.run(scenario())
