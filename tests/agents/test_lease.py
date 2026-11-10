"""租约：并发只有一个赢家、过期可接管、执行不会双跑。"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from apps.platform.contracts.ticket import TicketState

from apps.agents.lease import LeaseNotHeld
from apps.agents.runtime import ExecutionReport, PlannedStep

from dbutil import db_env, new_ticket, prepare_executing
from support import SCENE, ScriptedConnector, build_runtime, make_action


def test_only_one_owner_wins_concurrently(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store)
            results = await asyncio.gather(
                *[env.leases.acquire(ticket.id, f"worker-{index}") for index in range(8)]
            )
            assert sum(results) == 1
            holder, expires_at = await env.leases.holder(ticket.id)
            assert holder is not None and expires_at is not None

    asyncio.run(scenario())


def test_expired_lease_can_be_taken_over(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store)
            assert await env.leases.acquire(ticket.id, "worker-a", ttl_seconds=30)
            # 模拟执行者卡死：租约过期
            async with env.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.ticket SET lease_expires_at = now() - interval '1 second' "
                        "WHERE id = :id"
                    ),
                    {"id": ticket.id},
                )
            assert await env.leases.renew(ticket.id, "worker-a") is False
            assert await env.leases.acquire(ticket.id, "worker-b", ttl_seconds=30) is True
            holder, _ = await env.leases.holder(ticket.id)
            assert holder == "worker-b"

    asyncio.run(scenario())


def test_release_allows_next_worker(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store)
            assert await env.leases.acquire(ticket.id, "worker-a")
            assert await env.leases.acquire(ticket.id, "worker-b") is False
            assert await env.leases.release(ticket.id, "worker-a")
            assert await env.leases.acquire(ticket.id, "worker-b") is True

    asyncio.run(scenario())


def test_same_ticket_is_not_executed_twice(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector(write_delay=0.05)
            runtime = build_runtime(env, connector)
            step = PlannedStep(
                action=make_action("fake.draft.create"),
                target_system="fake",
                payload={"supplier": "SUP-1", "amount": "10.00"},
                idem_key="idem-single",
            )

            async def run(owner: str) -> ExecutionReport | Exception:
                try:
                    return await runtime.execute(
                        ticket.id, owner=owner, steps=[step], scene=SCENE
                    )
                except Exception as exc:  # noqa: BLE001
                    return exc

            results = await asyncio.gather(run("worker-a"), run("worker-b"))
            reports = [item for item in results if isinstance(item, ExecutionReport)]
            rejections = [item for item in results if isinstance(item, LeaseNotHeld)]

            assert len(reports) == 1
            assert len(rejections) == 1
            assert reports[0].status is TicketState.COMPLETED
            # 外部写入只发生一次（租约 + 步骤幂等共同保证）
            assert len(connector.writes) == 1
            assert len(await env.store.list_steps(ticket.id)) == 1

    asyncio.run(scenario())
