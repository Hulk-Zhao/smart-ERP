"""C15 scheduler：``erp.scheduler_lease`` 库内租约保证单点。

启动方式（compose 直接拉起）：

    python -m core.runtime.scheduler --lease-seconds 30 --tick-seconds 5

为什么不用进程内锁：多副本部署时每个副本各自持有一把进程内锁，都会认为
自己是唯一，等于没有单点。租约必须落在库里，其他副本能看到 ``owner`` 与
``expires_at``，持有者崩溃后过期即可被接管。

语义：

- 抢锁 / 续约同一条 ``INSERT ... ON CONFLICT DO UPDATE``：仅当「当前就是我」
  或「已过期」时才更新成功并 ``RETURNING``，否则没有行 = 没抢到；
- tick：回收租约过期的 ``running`` 任务（``JobQueueStore.reclaim_stale``），
  可选跑全链审计巡检（``--audit-verify``，断链会被 ``AuditGuard`` 冻结写入）；
- 退出：释放租约（``owner`` 置空、``expires_at`` 置 now），下一个副本立即接管。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.platform.config import get_settings
from apps.platform.db.session import build_engine, build_session_factory

from core.runtime.queue import JobQueueStore

logger = logging.getLogger("erp.scheduler")

DEFAULT_SCOPE = "scheduler"
# 租约 TTL 30s、tick 5s：循环内每 min(tick, lease/2)=5s 续约一次，
# 进程被强杀后最多 30s 另一个副本即可接管。
DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_TICK_SECONDS = 5.0

_ACQUIRE_SQL = text(
    """
    INSERT INTO erp.scheduler_lease AS l
        (scope, owner, expires_at, heartbeat_at, updated_at)
    VALUES (:scope, :owner, now() + make_interval(secs => :lease_seconds), now(), now())
    ON CONFLICT (scope) DO UPDATE
       SET owner = EXCLUDED.owner,
           expires_at = EXCLUDED.expires_at,
           heartbeat_at = now(),
           updated_at = now()
     WHERE l.owner = EXCLUDED.owner
        OR l.expires_at IS NULL
        OR l.expires_at < now()
    RETURNING l.owner
    """
)

_RELEASE_SQL = text(
    """
    UPDATE erp.scheduler_lease
       SET owner = NULL, expires_at = now(), heartbeat_at = now(), updated_at = now()
     WHERE scope = :scope AND owner = :owner
    """
)


@dataclass
class SchedulerStats:
    ticks: int = 0
    reclaimed: int = 0
    takeovers: int = 0


@dataclass
class Scheduler:
    session_factory: async_sessionmaker[AsyncSession]
    owner: str
    scope: str = DEFAULT_SCOPE
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    tick_seconds: float = DEFAULT_TICK_SECONDS
    audit_verify: bool = False
    jobs: JobQueueStore = field(init=False)
    stats: SchedulerStats = field(default_factory=SchedulerStats)
    _leader: bool = False
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0 or self.tick_seconds <= 0:
            raise ValueError("lease_seconds / tick_seconds 必须 > 0")
        # 续约间隔在 run() 内取 min(tick_seconds, lease_seconds/2)，
        # 即使 tick 配大了也不会出现「续约前租约先过期」。
        self.jobs = JobQueueStore(self.session_factory)

    # ---------------------------------------------------------------- 租约
    async def acquire(self) -> bool:
        """抢锁或续约；返回是否持有租约。"""

        async with self.session_factory() as session, session.begin():
            row = (
                await session.execute(
                    _ACQUIRE_SQL,
                    {
                        "scope": self.scope,
                        "owner": self.owner,
                        "lease_seconds": self.lease_seconds,
                    },
                )
            ).scalar_one_or_none()
        return row is not None

    async def release(self) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                _RELEASE_SQL, {"scope": self.scope, "owner": self.owner}
            )

    async def read_lease(self) -> dict[str, str | None] | None:
        """只读租约行，测试与运维排查用。"""

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT owner, expires_at, heartbeat_at "
                        "FROM erp.scheduler_lease WHERE scope = :scope"
                    ),
                    {"scope": self.scope},
                )
            ).mappings().one_or_none()
        return dict(row) if row else None

    # ---------------------------------------------------------------- tick
    async def tick(self) -> dict[str, int]:
        reclaimed = await self.jobs.reclaim_stale()
        self.stats.reclaimed += reclaimed
        result = {"reclaimed": reclaimed}
        if reclaimed:
            logger.warning("scheduler 回收 stale running 任务 %s 条", reclaimed)
        if self.audit_verify:
            from apps.agents.audit_guard import AuditGuard
            from apps.agents.store import TicketStore

            report = await AuditGuard(TicketStore(self.session_factory)).verify_all()
            result["audit_checked"] = report.checked
            result["audit_broken"] = report.broken
            logger.info(
                "审计巡检 checked=%s broken=%s degraded=%s elapsed_ms=%s",
                report.checked,
                report.broken,
                report.degraded,
                report.elapsed_ms,
            )
        return result

    # ---------------------------------------------------------------- 生命周期
    def stop(self) -> None:
        self._stop.set()

    async def run(
        self, *, max_ticks: int | None = None, install_signals: bool = False
    ) -> SchedulerStats:
        loop = asyncio.get_running_loop()
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, self.stop)
                except NotImplementedError:  # pragma: no cover - Windows 事件循环
                    signal.signal(sig, lambda *_: self.stop())

        interval = max(0.05, min(self.tick_seconds, self.lease_seconds / 2))
        logger.info(
            "scheduler %s 启动 scope=%s lease=%ss tick=%ss",
            self.owner,
            self.scope,
            self.lease_seconds,
            interval,
        )
        try:
            while not self._stop.is_set():
                if await self.acquire():
                    if not self._leader:
                        self._leader = True
                        self.stats.takeovers += 1
                        logger.info("scheduler %s 获得租约", self.owner)
                    await self.tick()
                    self.stats.ticks += 1
                    if max_ticks is not None and self.stats.ticks >= max_ticks:
                        break
                else:
                    if self._leader:
                        self._leader = False
                        logger.warning("scheduler %s 丢失租约", self.owner)
                    else:
                        logger.debug("scheduler %s 未抢到租约，等待", self.owner)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.release()
            logger.info(
                "scheduler %s 退出 ticks=%s reclaimed=%s",
                self.owner,
                self.stats.ticks,
                self.stats.reclaimed,
            )
        return self.stats


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="C15 scheduler：库内租约单点调度")
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument(
        "--owner", default=f"{socket.gethostname()}:{os.getpid()}:scheduler"
    )
    parser.add_argument("--lease-seconds", type=float, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--tick-seconds", type=float, default=DEFAULT_TICK_SECONDS)
    parser.add_argument(
        "--audit-verify", action="store_true", help="每次 tick 跑全链审计巡检"
    )
    parser.add_argument("--ticks", type=int, default=None, help="跑 N 个 tick 后退出")
    parser.add_argument(
        "--once", action="store_true", help="等价于 --ticks 1（冒烟/复验用）"
    )
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> SchedulerStats:
    engine = build_engine(get_settings())
    try:
        scheduler = Scheduler(
            session_factory=build_session_factory(engine),
            owner=args.owner,
            scope=args.scope,
            lease_seconds=args.lease_seconds,
            tick_seconds=args.tick_seconds,
            audit_verify=args.audit_verify,
        )
        max_ticks = 1 if args.once else args.ticks
        return await scheduler.run(max_ticks=max_ticks, install_signals=True)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        level=os.environ.get("ERP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_amain(_parse_args(argv)))


if __name__ == "__main__":
    main()
