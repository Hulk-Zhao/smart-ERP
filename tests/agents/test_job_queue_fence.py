"""job_queue 代次围栏与 ``run_at`` 可空的契约用例。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from core.runtime.queue import JobQueueStore, LeaseLostError
from tests.agents.dbutil import db_env


def _row_sql() -> str:
    return "SELECT state, lease_owner, lease_gen FROM erp.job_queue WHERE id = :id"


def test_claim_bumps_lease_gen_and_rejects_stale_complete(db: str) -> None:
    """同一个 owner 被回收后重新领回，旧代次的 complete 必须被显式拒绝。"""

    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            job_id = await queue.enqueue("agent", "echo")

            first = await queue.claim(["agent"], owner="w-same", lease_seconds=0.05)
            assert first is not None and first.lease_gen == 1

            await asyncio.sleep(0.1)
            assert await queue.reclaim_stale() == 1  # 过期回 queued、owner 清空

            # 同一个 worker_id 再领一次：owner 相同，只有 lease_gen 能区分两代执行
            second = await queue.claim(["agent"], owner="w-same", lease_seconds=30)
            assert second is not None and second.id == job_id
            assert second.lease_gen == 2

            with pytest.raises(LeaseLostError) as err:
                await queue.complete(job_id, owner="w-same", lease_gen=first.lease_gen)
            assert "lease_gen=1" in str(err.value)
            assert "lease_gen=2" in str(err.value)

            # 旧代次被拒后行仍是新一轮的 running，没被旧执行写成 succeeded
            async with env.sf() as session:
                row = (
                    await session.execute(text(_row_sql()), {"id": job_id})
                ).mappings().one()
            assert (row["state"], row["lease_owner"], row["lease_gen"]) == (
                "running",
                "w-same",
                2,
            )

            await queue.complete(job_id, owner="w-same", lease_gen=second.lease_gen)
            async with env.sf() as session:
                state = (
                    await session.execute(
                        text("SELECT state FROM erp.job_queue WHERE id = :id"),
                        {"id": job_id},
                    )
                ).scalar_one()
            assert state == "succeeded"

    asyncio.run(_run())


def test_heartbeat_and_fail_reject_stale_gen(db: str) -> None:
    """续租与失败落账同样必须带代次；不匹配即显式失败且不改行。"""

    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            job_id = await queue.enqueue("agent", "echo")
            stale = await queue.claim(["agent"], owner="w-old", lease_seconds=0.05)
            assert stale is not None

            await asyncio.sleep(0.1)
            assert await queue.reclaim_stale() == 1
            fresh = await queue.claim(["agent"], owner="w-new", lease_seconds=30)
            assert fresh is not None and fresh.lease_gen == stale.lease_gen + 1

            with pytest.raises(LeaseLostError):
                await queue.heartbeat_lease(
                    job_id,
                    owner="w-old",
                    lease_gen=stale.lease_gen,
                    lease_seconds=30,
                )
            with pytest.raises(LeaseLostError):
                await queue.fail(
                    job_id,
                    owner="w-old",
                    lease_gen=stale.lease_gen,
                    error="boom",
                )

            # 旧执行的心跳/失败落账都没生效：行仍归新执行
            async with env.sf() as session:
                row = (
                    await session.execute(text(_row_sql()), {"id": job_id})
                ).mappings().one()
            assert (row["state"], row["lease_owner"], row["lease_gen"]) == (
                "running",
                "w-new",
                fresh.lease_gen,
            )

    asyncio.run(_run())


def test_enqueue_without_delay_writes_null_run_at(db: str) -> None:
    """入队无延迟必须写 NULL，而不是 now()+0：否则回拨又把任务藏进未来。"""

    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            job_id = await queue.enqueue("agent", "echo")
            async with env.sf() as session:
                run_at = (
                    await session.execute(
                        text("SELECT run_at FROM erp.job_queue WHERE id = :id"),
                        {"id": job_id},
                    )
                ).scalar_one()
            assert run_at is None

    asyncio.run(_run())


def test_fail_with_zero_backoff_writes_null_run_at(db: str) -> None:
    """backoff=0 的失败结算写 NULL：库时钟回拨 1.4s 时仍能被 claim。

    「回拨」的模拟：另一行的 run_at 是「回拨前写的 now()+1.4s」，回拨后看起来在未来；
    NULL 行不受影响——这条用例是 0011 值得迁移的直接证据。
    """

    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            job_id = await queue.enqueue("agent", "echo")
            claimed = await queue.claim(["agent"], owner="w-fail", lease_seconds=30)
            assert claimed is not None and claimed.id == job_id

            state = await queue.fail(
                job_id,
                owner="w-fail",
                lease_gen=claimed.lease_gen,
                error="boom",
                backoff_seconds=0.0,
            )
            assert state == "queued"
            async with env.sf() as session:
                run_at = (
                    await session.execute(
                        text("SELECT run_at FROM erp.job_queue WHERE id = :id"),
                        {"id": job_id},
                    )
                ).scalar_one()
            assert run_at is None, "backoff=0 必须写 NULL，不能写 now()+0"

            # 模拟回拨 1.4s 之后的时间戳：高优先级但 run_at 在「未来」的行不可领
            hidden_id = await queue.enqueue("agent", "echo", priority=9)
            async with env.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.job_queue "
                        "SET run_at = now() + interval '1.4 seconds' WHERE id = :id"
                    ),
                    {"id": hidden_id},
                )

            again = await queue.claim(["agent"], owner="w-after", lease_seconds=30)
            assert again is not None and again.id == job_id
            async with env.sf() as session:
                hidden_state = (
                    await session.execute(
                        text("SELECT state FROM erp.job_queue WHERE id = :id"),
                        {"id": hidden_id},
                    )
                ).scalar_one()
            assert hidden_state == "queued", "回拨期间 run_at 在未来的行应被隐藏"

    asyncio.run(_run())


def test_reclaim_requeue_with_zero_backoff_writes_null_run_at(db: str) -> None:
    """回收重排（backoff=0）同样写 NULL，避免回拨把刚回收的任务藏起来。"""

    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            job_id = await queue.enqueue("agent", "echo")
            claimed = await queue.claim(["agent"], owner="w-slow", lease_seconds=0.05)
            assert claimed is not None

            await asyncio.sleep(0.1)
            assert await queue.reclaim_stale(backoff_seconds=0.0) == 1
            async with env.sf() as session:
                run_at = (
                    await session.execute(
                        text("SELECT run_at FROM erp.job_queue WHERE id = :id"),
                        {"id": job_id},
                    )
                ).scalar_one()
            assert run_at is None

    asyncio.run(_run())


def test_run_at_null_is_immediately_claimable(db: str) -> None:
    """NULL = 立即可领取；未来的 run_at 即使 priority 更高也不能被领。"""

    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            async with env.sf() as session, session.begin():
                immediate_id = (
                    await session.execute(
                        text(
                            "INSERT INTO erp.job_queue "
                            "(queue, kind, payload, priority, run_at) "
                            "VALUES ('agent', 'echo', '{}'::jsonb, 0, NULL) "
                            "RETURNING id"
                        )
                    )
                ).scalar_one()
                future_id = (
                    await session.execute(
                        text(
                            "INSERT INTO erp.job_queue "
                            "(queue, kind, payload, priority, run_at) "
                            "VALUES ('agent', 'echo', '{}'::jsonb, 9, "
                            "now() + interval '1 hour') RETURNING id"
                        )
                    )
                ).scalar_one()

            job = await queue.claim(["agent"], owner="w-null", lease_seconds=30)
            assert job is not None and job.id == immediate_id
            assert job.lease_gen == 1

            async with env.sf() as session:
                future_state = (
                    await session.execute(
                        text("SELECT state FROM erp.job_queue WHERE id = :id"),
                        {"id": future_id},
                    )
                ).scalar_one()
            assert future_state == "queued"

    asyncio.run(_run())
