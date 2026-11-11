"""补偿执行器：discovery 口径、owner 守卫、stale 重认领、执行期续期。

真实容器验收（倒序、kill -9、外部 /metrics）不在这里替代，见卡片。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime

from dbutil import db_env, prepare_executing
from fastapi.testclient import TestClient
from sqlalchemy import text
from support import SCENE, ScriptedConnector, build_runtime, make_action

from apps.agents.compensation_worker import CompensationWorker, build_http_app
from apps.agents.runtime import PlannedStep
from apps.platform.connectors.sdk import CompensateResult
from apps.platform.contracts.ticket import TicketState
from observability.metrics import MetricsRegistry

PAYLOAD = {"supplier": "SUP-1", "amount": "10.00"}

# 续期判据的固定基准：宿主机与 PG 的墙钟会回拨（实测 150s 内 11 次、最大 -2.19s），
# 所以不能比较两次采样值的严格递增；先把心跳钉到过去、租约钉到远期，续期一旦发生
# 必然把它们改写成 now() 派生值，断言只判断「写过没有」，与时钟是否单调无关。
_HEARTBEAT_SEED = datetime(2000, 1, 1, tzinfo=UTC)
_LEASE_SEED = datetime(2100, 1, 1, tzinfo=UTC)


def _step(key: str) -> PlannedStep:
    return PlannedStep(
        action=make_action(key),
        target_system="fake",
        payload=PAYLOAD,
        idem_key=f"idem-{key}",
    )


async def _compensating_ticket(env, *, step_count: int = 4, fail_last: bool = True):
    """执行到最后一步失败，拿到「补偿中」工单；补偿行按 seq 倒序排队。"""

    ticket = await prepare_executing(env.store)
    steps = [_step(f"s{i}") for i in range(1, step_count + 1)]
    behaviour = {f"s{step_count}": "transient"} if fail_last else {}
    connector = ScriptedConnector(behaviours=behaviour)
    runtime = build_runtime(env, connector, max_attempts=1)
    report = await runtime.execute(ticket.id, owner="exec", steps=steps, scene=SCENE)
    assert report.status is TicketState.COMPENSATING
    return ticket, connector, runtime


def _worker(env, runtime, *, stale: float = 60.0, limit: int = 20):
    return CompensationWorker(
        runtime=runtime,
        session_factory=env.sf,
        metrics=MetricsRegistry(),
        owner="test-owner",
        poll_seconds=0.05,
        stale_seconds=stale,
        discovery_limit=limit,
    )


def test_http_app_serves_metrics_and_healthz() -> None:
    metrics = MetricsRegistry()
    worker = CompensationWorker(
        runtime=None,  # type: ignore[arg-type] - 只测 HTTP 面
        session_factory=None,  # type: ignore[arg-type]
        metrics=metrics,
        owner="test",
    )
    client = TestClient(build_http_app(worker, metrics))

    assert client.get("/metrics").status_code == 200
    assert client.get("/healthz").status_code == 503

    worker.status.last_tick_at = time.monotonic()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_discovery_is_driven_by_due_queue_rows(db: str) -> None:
    """队列行是唯一事实来源：不按工单/步骤状态反推。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket, _connector, runtime = await _compensating_ticket(env)
            worker = _worker(env, runtime)
            assert await worker._discover() == [ticket.id]

            # 未进「补偿中」的工单，只要队列里有到期行就照样消费：
            # 这正是 API 同步回滚不回写 ticket_step.state 时不能按状态扫的原因。
            other = await prepare_executing(env.store)
            step = await env.store.intent_step(
                ticket_id=other.id,
                step_no=1,
                action_key="fake.draft.create.1",
                target_system="fake",
                write_level=1,
                idem_key="idem-other-1",
                request_hash=hashlib.sha256(b"{}").hexdigest(),
                request_snapshot={},
            )
            await env.store.mark_step(step_id=step.id, state="succeeded")
            await env.queue.enqueue(other.id)
            assert set(await worker._discover()) == {ticket.id, other.id}

            # 未来重试闸门未到期的行不选。
            async with env.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.compensation_task "
                        "SET next_retry_at = now() + interval '60 seconds' "
                        "WHERE ticket_id = :ticket_id"
                    ),
                    {"ticket_id": ticket.id},
                )
            assert await worker._discover() == [other.id]

            async with env.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.compensation_task SET next_retry_at = NULL "
                        "WHERE ticket_id = :ticket_id"
                    ),
                    {"ticket_id": ticket.id},
                )
            assert set(await worker._discover()) == {ticket.id, other.id}

    asyncio.run(scenario())


def test_mark_done_and_failed_respect_owner(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket, _connector, _runtime = await _compensating_ticket(env)
            task = await env.queue.claim_next(ticket.id, "owner-A")
            assert task is not None

            await env.queue.mark_done(task.id, owner="owner-B")
            row = await _fetch_task(env, task.id)
            assert row.state == "running" and row.lease_owner == "owner-A"

            await env.queue.mark_failed(
                task.id, "boom", max_attempts=1, owner="owner-B"
            )
            row = await _fetch_task(env, task.id)
            assert row.state == "running"

            await env.queue.mark_done(task.id, owner="owner-A")
            assert (await _fetch_task(env, task.id)).state == "done"

    asyncio.run(scenario())


def test_stale_reclaim_keeps_attempts_and_clears_lease(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket, _connector, runtime = await _compensating_ticket(env)
            task = await env.queue.claim_next(ticket.id, "owner-A")
            assert task is not None and task.attempts == 1
            async with env.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.compensation_task "
                        "SET heartbeat_at = now() - interval '120 seconds' "
                        "WHERE id = :task_id"
                    ),
                    {"task_id": task.id},
                )

            reclaimed = await runtime.recover_stale_compensations(
                stale_after_seconds=60
            )
            assert [item.id for item in reclaimed] == [task.id]

            row = await _fetch_task(env, task.id)
            assert row.state == "queued"
            assert row.lease_owner is None
            assert row.next_retry_at is None
            assert row.attempts == 1

            again = await env.queue.claim_next(ticket.id, "owner-B")
            assert again is not None and again.attempts == 2

    asyncio.run(scenario())


def test_long_call_renews_task_heartbeat_and_ticket_lease(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            steps = [_step(f"s{i}") for i in range(1, 3)]
            connector = ScriptedConnector(behaviours={"s2": "transient"})
            gate = asyncio.Event()

            async def slow_compensator(call, idem_key):
                gate.set()
                await asyncio.sleep(2.4)
                return CompensateResult(status="compensated")

            runtime = build_runtime(
                env,
                connector,
                max_attempts=1,
                compensator=slow_compensator,
                compensation_stale_seconds=3.0,
                lease_ttl_seconds=3,
            )
            report = await runtime.execute(
                ticket.id, owner="exec", steps=steps, scene=SCENE
            )
            assert report.status is TicketState.COMPENSATING

            worker = _worker(env, runtime, stale=3.0)
            tick = asyncio.create_task(worker.tick())
            await asyncio.wait_for(gate.wait(), timeout=3)

            # 心跳/租约此刻已有初值（认领与 acquire 写入）。把它们钉到固定基准，
            # 之后任何一次续期都会覆盖成 now() 派生值：判据是「被改写」，
            # 不是「第二次采样的墙钟大于第一次」。
            await _pin_renewal_markers(env, ticket.id)
            heartbeat_at, lease_owner, lease_expires_at = await _wait_renewal(
                env, ticket.id
            )
            assert lease_owner == "test-owner"
            assert heartbeat_at > _HEARTBEAT_SEED, f"任务心跳未被续期：{heartbeat_at!r}"
            assert lease_expires_at < _LEASE_SEED, f"工单租约未被续期：{lease_expires_at!r}"

            await asyncio.wait_for(tick, timeout=10)
            assert (
                await env.store.get_ticket(ticket.id)
            ).state == TicketState.ROLLED_BACK

    asyncio.run(scenario())


async def _fetch_task(env, task_id):
    async with env.sf() as session:
        return (
            await session.execute(
                text(
                    "SELECT state, lease_owner, next_retry_at, attempts "
                    "FROM erp.compensation_task WHERE id = :task_id"
                ),
                {"task_id": task_id},
            )
        ).one()


async def _fetch_renewal(env, ticket_id):
    async with env.sf() as session:
        task = (
            await session.execute(
                text(
                    "SELECT heartbeat_at, lease_owner FROM erp.compensation_task "
                    "WHERE ticket_id = :ticket_id AND state = 'running'"
                ),
                {"ticket_id": ticket_id},
            )
        ).one_or_none()
        if task is None:
            return None
        lease = (
            await session.execute(
                text("SELECT lease_expires_at FROM erp.ticket WHERE id = :ticket_id"),
                {"ticket_id": ticket_id},
            )
        ).one()
    return task.heartbeat_at, task.lease_owner, lease.lease_expires_at


async def _pin_renewal_markers(env, ticket_id) -> None:
    """把续期目标钉到固定基准，让「是否续期过」可判定且不依赖墙钟单调。"""

    async with env.sf() as session, session.begin():
        pinned = (
            await session.execute(
                text(
                    "UPDATE erp.compensation_task SET heartbeat_at = :seed "
                    "WHERE ticket_id = :ticket_id AND state = 'running' "
                    "RETURNING id"
                ),
                {"seed": _HEARTBEAT_SEED, "ticket_id": ticket_id},
            )
        ).all()
        assert pinned, "补偿行不在 running，续期用例没有可观察对象"
        await session.execute(
            text("UPDATE erp.ticket SET lease_expires_at = :seed WHERE id = :ticket_id"),
            {"seed": _LEASE_SEED, "ticket_id": ticket_id},
        )


async def _wait_renewal(env, ticket_id, *, timeout: float = 5.0):
    """等两个基准值都被续期覆盖；超时把最后观察到的实际值带进断言消息。"""

    deadline = time.monotonic() + timeout
    row = await _fetch_renewal(env, ticket_id)
    while not (
        row is not None
        and row[0] is not None
        and row[0] > _HEARTBEAT_SEED
        and row[2] is not None
        and row[2] < _LEASE_SEED
    ):
        if time.monotonic() >= deadline:
            observed = "补偿行已结束（running 行消失）" if row is None else row
            raise AssertionError(f"{timeout}s 内未观察到续期，实际值：{observed!r}")
        await asyncio.sleep(0.05)
        row = await _fetch_renewal(env, ticket_id)
    return row
