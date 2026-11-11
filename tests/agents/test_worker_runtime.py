"""C15 常驻进程：队列真消费、落库心跳、stale 回收、scheduler 单点租约。

所有数字都从 ``erp.job_queue`` / ``erp.worker_heartbeat`` / ``erp.scheduler_lease``
读回，不读进程内变量——S8 复验看的也是这几张表。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from core.runtime.queue import JobQueueStore
from core.runtime.scheduler import Scheduler
from core.runtime.worker import HANDLERS, Worker
from tests.agents.dbutil import db_env
from tools.health import HealthConfig, check_once, run_check


async def _enqueue(
    store: JobQueueStore, count: int, kind: str = "echo", **kwargs: object
) -> list[int]:
    return [await store.enqueue("agent", kind, **kwargs) for _ in range(count)]


def test_worker_consumes_queue_and_writes_heartbeat(db: str) -> None:
    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            ids = await _enqueue(queue, 5)
            worker = Worker(
                session_factory=env.sf,
                queues=["agent"],
                worker_id="w-done",
                concurrency=2,
                lease_seconds=5.0,
                heartbeat_seconds=0.05,
                poll_seconds=0.02,
            )
            stats = await worker.run(max_jobs=5)

            assert stats.done == 5, stats
            assert stats.failed == 0, stats

            async with env.sf() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT state, count(*) AS n FROM erp.job_queue "
                            "WHERE id = ANY(:ids) GROUP BY state"
                        ),
                        {"ids": ids},
                    )
                ).all()
                assert dict(rows) == {"succeeded": 5}

                beat = (
                    await session.execute(
                        text(
                            "SELECT state, jobs_done, jobs_failed, "
                            "extract(epoch FROM now() - heartbeat_at) AS age "
                            "FROM erp.worker_heartbeat WHERE worker_id = 'w-done'"
                        )
                    )
                ).one()
                assert beat.state == "stopped"
                assert beat.jobs_done == 5
                assert beat.jobs_failed == 0
                assert float(beat.age) < 60

    asyncio.run(_run())


def test_failed_job_retries_then_dead(db: str) -> None:
    async def _boom(_job: object) -> None:
        raise RuntimeError("boom")

    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            job_id = await queue.enqueue("agent", "boom", max_attempts=2)
            worker = Worker(
                session_factory=env.sf,
                queues=["agent"],
                worker_id="w-fail",
                concurrency=1,
                lease_seconds=5.0,
                heartbeat_seconds=0.05,
                poll_seconds=0.01,
                retry_backoff_seconds=0.01,
                handlers={**HANDLERS, "boom": _boom},
            )
            stats = await worker.run(max_jobs=2)

            # 两次尝试都失败：一次回队列重试，第二次到上限转 dead（明确终态）
            assert stats.failed == 2, stats
            assert stats.done == 0

            async with env.sf() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT state, attempts, max_attempts, last_error "
                            "FROM erp.job_queue WHERE id = :id"
                        ),
                        {"id": job_id},
                    )
                ).one()
                assert row.state == "dead"
                assert row.attempts == 2
                assert row.max_attempts == 2
                assert "boom" in (row.last_error or "")

    asyncio.run(_run())


def test_unknown_kind_goes_dead_without_retry(db: str) -> None:
    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            job_id = await queue.enqueue("agent", "no-such-handler")
            worker = Worker(
                session_factory=env.sf,
                queues=["agent"],
                worker_id="w-unknown",
                concurrency=1,
                poll_seconds=0.01,
                heartbeat_seconds=0.05,
            )
            stats = await worker.run(max_jobs=1)
            assert stats.failed == 1

            async with env.sf() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT state, attempts FROM erp.job_queue WHERE id = :id"
                        ),
                        {"id": job_id},
                    )
                ).one()
                assert row.state == "dead"
                assert row.attempts == 1  # 重试也不会好，不转圈

    asyncio.run(_run())


def test_killed_worker_heartbeat_stops_then_job_is_reclaimable(db: str) -> None:
    """worker 被杀 → 心跳停 → 探针失败 → 租约过期 → 任务重新可认领。"""

    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            job_id = await queue.enqueue("agent", "echo")

            # 模拟一个已被 kill -9 的 worker：认领后不再心跳
            job = await queue.claim(["agent"], owner="dead-worker", lease_seconds=0.05)
            assert job is not None and job.id == job_id
            async with env.sf() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO erp.worker_heartbeat "
                        "(worker_id, queues, state, started_at, heartbeat_at) "
                        "VALUES ('dead-worker', ARRAY['agent'], 'busy', "
                        "now() - interval '10 minutes', now() - interval '10 minutes')"
                    )
                )

            config = HealthConfig(
                target="worker", database_url=db, stale_seconds=300.0
            )
            before = await check_once(config)
            assert not before.ok, "心跳已停 10 分钟，探针必须失败"

            await asyncio.sleep(0.1)
            reclaimed = await queue.reclaim_stale()
            assert reclaimed == 1

            new_job = await queue.claim(
                ["agent"], owner="fresh-worker", lease_seconds=5.0
            )
            assert new_job is not None and new_job.id == job_id

            async with env.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.worker_heartbeat SET heartbeat_at = now() "
                        "WHERE worker_id = 'dead-worker'"
                    )
                )
            after = await check_once(config)
            assert after.ok, after.payload

    asyncio.run(_run())


def test_scheduler_lease_is_single_point_and_takeover(db: str) -> None:
    async def _run() -> None:
        async with db_env(db) as env_a, db_env(db) as env_b:
            first = Scheduler(
                session_factory=env_a.sf,
                owner="scheduler-a",
                scope="unit-test",
                lease_seconds=300.0,
            )
            second = Scheduler(
                session_factory=env_b.sf,
                owner="scheduler-b",
                scope="unit-test",
                lease_seconds=300.0,
            )
            assert await first.acquire() is True

            # 「未过期」用显式状态表示，不靠 200ms 窗口去赌两次 acquire 之间的耗时：
            # 单次 acquire 在本地实测 p50≈18ms、p95≈29ms，满载时可能越过 200ms
            # （实测 277ms 的间隔就会让 second 抢到），而 Docker Desktop 的时钟
            # 跳变也会让任何亚秒级窗口失真。infinity 不会被 now() 越过。
            async with env_a.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.scheduler_lease SET expires_at = 'infinity' "
                        "WHERE scope = 'unit-test'"
                    )
                )
            assert await second.acquire() is False, "租约未过期时第二个副本不能拿到"

            lease = await second.read_lease()
            assert lease is not None and lease["owner"] == "scheduler-a"

            # 持有者崩溃：把租约显式标成过期（-infinity），不依赖真实等待，
            # 也不怕时钟回拨把「1 秒前」重新推回未来。
            async with env_a.sf() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE erp.scheduler_lease SET expires_at = '-infinity' "
                        "WHERE scope = 'unit-test'"
                    )
                )
            assert await second.acquire() is True
            assert await first.acquire() is False, "被接管后原持有者不能续约"

            # 释放后下一个副本立即接管：``owner`` 为空即可，不要求 now() 已越过
            # release 写下的 expires_at（否则一次回拨就能让已释放的租约拿不回来）。
            await second.release()
            assert await first.acquire() is True

    asyncio.run(_run())


def test_scheduler_tick_reclaims_stale_jobs(db: str) -> None:
    async def _run() -> None:
        async with db_env(db) as env:
            queue = JobQueueStore(env.sf)
            await queue.enqueue("agent", "echo")
            claimed = await queue.claim(["agent"], owner="dead", lease_seconds=0.05)
            assert claimed is not None
            await asyncio.sleep(0.1)

            scheduler = Scheduler(
                session_factory=env.sf,
                owner="scheduler-tick",
                scope="tick-test",
                lease_seconds=5.0,
            )
            assert await scheduler.acquire() is True
            result = await scheduler.tick()
            assert result["reclaimed"] == 1
            await scheduler.release()

    asyncio.run(_run())


def test_scheduler_runs_ticks_then_releases_lease(db: str) -> None:
    async def _run() -> None:
        async with db_env(db) as env:
            scheduler = Scheduler(
                session_factory=env.sf,
                owner="scheduler-run",
                scope="run-test",
                lease_seconds=0.4,
                tick_seconds=0.05,
            )
            stats = await scheduler.run(max_ticks=2)
            assert stats.ticks == 2
            assert stats.takeovers == 1

            async with env.sf() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT owner FROM erp.scheduler_lease "
                            "WHERE scope = 'run-test'"
                        )
                    )
                ).one()
                assert row.owner is None, "退出必须释放租约，不能把锁带到下一个进程"

    asyncio.run(_run())


def test_health_scheduler_reports_expired_lease(db: str) -> None:
    async def _run() -> None:
        async with db_env(db) as env:
            scheduler = Scheduler(
                session_factory=env.sf,
                owner="scheduler-health",
                scope="health-test",
                lease_seconds=0.1,
            )
            assert await scheduler.acquire() is True
            config = HealthConfig(
                target="scheduler", database_url=db, scope="health-test"
            )
            assert (await check_once(config)).ok

            await asyncio.sleep(0.15)
            expired = await check_once(config)
            assert not expired.ok
            assert "过期" in expired.payload.get("reason", "")

            await scheduler.release()

    asyncio.run(_run())


def test_health_sync_check_mode_returns_failure_without_heartbeat(db: str) -> None:
    """CLI ``--check`` 走同步路径：没有心跳时必须失败（退出码 1）。"""

    result = run_check(HealthConfig(target="worker", database_url=db))
    assert not result.ok
    assert "心跳" in result.payload.get("reason", "")
