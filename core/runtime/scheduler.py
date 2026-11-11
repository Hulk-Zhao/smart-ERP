"""C15 scheduler：``erp.scheduler_lease`` 库内租约保证单点。

启动方式（compose 直接拉起）：

    python -m core.runtime.scheduler --lease-seconds 30 --tick-seconds 5

为什么不用进程内锁：多副本部署时每个副本各自持有一把进程内锁，都会认为
自己是唯一，等于没有单点。租约必须落在库里，其他副本能看到 ``owner`` 与
``expires_at``，持有者崩溃后过期即可被接管。

语义：

- 抢锁 / 续约同一条 ``INSERT ... ON CONFLICT DO UPDATE``：仅当「当前就是我」
  或「``owner`` 为空（已释放）」或「已过期」时才更新成功并 ``RETURNING``，
  否则没有行 = 没抢到。``owner`` 为空即可接管，不要求 ``now()`` 已越过
  ``expires_at``——释放后的接管不能依赖时钟单调前进；
- tick：回收租约过期的 ``running`` 任务（``JobQueueStore.reclaim_stale``），
  可选跑全链审计巡检（``--audit-verify``，断链会被 ``AuditGuard`` 冻结写入）；
  默认还会每 60s 跑一个 ``audit_image`` 链校验时间片（``--no-audit-image-scan``
  可关），结论写入 ``smart_erp_audit_chain_broken``；
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

from apps.audit.ledger import AuditLedger
from apps.audit.monitor import AuditChainMonitor
from apps.audit.store import SqlAuditStore
from apps.platform.config import get_settings
from apps.platform.db.session import build_engine, build_session_factory
from core.runtime.queue import DEFAULT_RECLAIM_GRACE_SECONDS, JobQueueStore
from observability.metrics import MetricsRegistry

logger = logging.getLogger("erp.scheduler")

DEFAULT_SCOPE = "scheduler"
# 租约 TTL 30s、tick 5s：循环内每 min(tick, lease/2)=5s 续约一次，
# 进程被强杀后最多 30s 另一个副本即可接管。
DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_TICK_SECONDS = 5.0
# 审计影像链巡检：每 60s 跑一个时间片、每片最多 2000 行、每页 200 行。
# 依据（台账「10 万行 44 秒」≈0.44ms/行）：2000 行约 0.9s，scheduler 容器 96MiB
# 下单页 200 行的瞬时占用约几 MB；10 万行一轮约 50 个时间片 ≈ 50min。
# 周期用 time.monotonic 计时，不受服务器时钟回拨影响。
DEFAULT_AUDIT_IMAGE_INTERVAL = 60.0
DEFAULT_AUDIT_IMAGE_ROWS = 2000
DEFAULT_AUDIT_IMAGE_PAGE = 200

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
        OR l.owner IS NULL
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
    audit_scans: int = 0
    audit_rows: int = 0
    audit_breaks: int = 0
    audit_errors: int = 0


@dataclass
class Scheduler:
    session_factory: async_sessionmaker[AsyncSession]
    owner: str
    scope: str = DEFAULT_SCOPE
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    tick_seconds: float = DEFAULT_TICK_SECONDS
    # 回收宽限：scheduler 不持有 job 租约，按任务租约的续约节奏（默认 60/3）取值，
    # 避免库时钟前跳时把还活着的 worker 的任务判死。测试可置 0 走严格语义。
    reclaim_grace_seconds: float = DEFAULT_RECLAIM_GRACE_SECONDS
    # tick 的来源（cli/env/default），只用于启动日志对账，不参与调度决策。
    tick_source: str = "default"
    audit_verify: bool = False
    audit_image_scan: bool = True
    audit_image_rows: int = DEFAULT_AUDIT_IMAGE_ROWS
    audit_image_interval_seconds: float = DEFAULT_AUDIT_IMAGE_INTERVAL
    metrics: MetricsRegistry | None = None
    jobs: JobQueueStore = field(init=False)
    audit_chain: AuditChainMonitor | None = field(default=None, init=False)
    stats: SchedulerStats = field(default_factory=SchedulerStats)
    _leader: bool = False
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0 or self.tick_seconds <= 0:
            raise ValueError("lease_seconds / tick_seconds 必须 > 0")
        # 续约间隔在 run() 内取 min(tick_seconds, lease_seconds/2)，
        # 即使 tick 配大了也不会出现「续约前租约先过期」。
        self.jobs = JobQueueStore(self.session_factory)
        if self.metrics is None:
            self.metrics = MetricsRegistry()
        if self.audit_image_scan:
            self.audit_chain = AuditChainMonitor(
                ledger=AuditLedger(store=SqlAuditStore(self.session_factory)),
                sink=self.metrics,
                rows_per_run=self.audit_image_rows,
                page_size=DEFAULT_AUDIT_IMAGE_PAGE,
                interval_seconds=self.audit_image_interval_seconds,
            )

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
        # scheduler 不认领 job_queue，exclude_owner 只是防御（同名 owner 也不会误伤自己）。
        reclaimed = await self.jobs.reclaim_stale(
            exclude_owner=self.owner,
            grace_seconds=self.reclaim_grace_seconds,
        )
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
        if self.audit_chain is not None:
            try:
                scan = await self.audit_chain.step()
            except Exception:
                # 校验本身失败时绝不写 0：那会把「不知道」变成「没问题」。
                # 保持上一次指标值，只把失败记在 stats/日志里，由外部告警兜底。
                self.stats.audit_errors += 1
                logger.exception(
                    "审计影像链巡检失败：保持上一次 smart_erp_audit_chain_broken 不变"
                )
                result["audit_image_error"] = 1
            else:
                if scan is not None:
                    self.stats.audit_scans += 1
                    self.stats.audit_rows += scan.rows
                    result["audit_image_rows"] = scan.rows
                    if scan.broken_at is not None:
                        self.stats.audit_breaks += 1
                        logger.error(
                            "审计链断裂 tenant=%s broken_at=%s rows=%s detail=%s",
                            scan.tenant_id,
                            scan.broken_at,
                            scan.rows,
                            scan.detail,
                        )
                    else:
                        logger.info(
                            "审计影像链巡检 tenant=%s rows=%s cycle_completed=%s elapsed_ms=%.1f",
                            scan.tenant_id,
                            scan.rows,
                            scan.cycle_completed,
                            scan.elapsed_ms,
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
        # 三个数一起打：配置值 / 租约 / 实际生效间隔。注释会腐烂，这行日志不会——
        # 「我设了 60 为什么 15 秒一跳」的答案必须在 docker logs 里。
        logger.info(
            "scheduler %s 启动 scope=%s tick_seconds=%s source=%s lease=%ss effective=%ss",
            self.owner,
            self.scope,
            self.tick_seconds,
            self.tick_source,
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


_TICK_ENV = "ERP_SCHEDULER_TICK_SECONDS"


def _tick_seconds_from_env() -> tuple[float, str]:
    """未给 --tick-seconds 时读环境变量，返回 (秒数, 来源)。

    空值回落默认 5s；非数字或 ≤0 直接抛 SystemExit 并带上键名，不静默回落——
    compose 里写着一个看似生效的值而实际不生效，比没有这个变量更危险。
    """
    raw = os.environ.get(_TICK_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TICK_SECONDS, "default"
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(
            f"{_TICK_ENV}={raw!r} 不是数字；留空或删除该变量则使用默认 "
            f"{DEFAULT_TICK_SECONDS}s"
        ) from None
    if value <= 0:
        raise SystemExit(f"{_TICK_ENV}={raw!r} 必须 > 0")
    return value, "env"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="C15 scheduler：库内租约单点调度")
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument(
        "--owner", default=f"{socket.gethostname()}:{os.getpid()}:scheduler"
    )
    parser.add_argument("--lease-seconds", type=float, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument(
        "--tick-seconds",
        type=float,
        default=None,
        help=(
            f"tick 间隔秒数；未指定时依次取 {_TICK_ENV}、默认 {DEFAULT_TICK_SECONDS}s"
            "（实际循环间隔另有 min(tick, lease/2) 上限）"
        ),
    )
    parser.add_argument(
        "--audit-verify", action="store_true", help="每次 tick 跑全链审计巡检"
    )
    parser.add_argument(
        "--no-audit-image-scan",
        action="store_true",
        help="关闭 audit_image 链周期巡检（默认开启，写入 smart_erp_audit_chain_broken）",
    )
    parser.add_argument(
        "--audit-image-rows",
        type=int,
        default=DEFAULT_AUDIT_IMAGE_ROWS,
        help="每个时间片最多校验的行数（默认 %(default)s）",
    )
    parser.add_argument(
        "--audit-image-interval-seconds",
        type=float,
        default=DEFAULT_AUDIT_IMAGE_INTERVAL,
        help="两个校验时间片的最小间隔秒数（默认 %(default)s）",
    )
    parser.add_argument("--ticks", type=int, default=None, help="跑 N 个 tick 后退出")
    parser.add_argument(
        "--once", action="store_true", help="等价于 --ticks 1（冒烟/复验用）"
    )
    args = parser.parse_args(argv)
    if args.tick_seconds is None:
        args.tick_seconds, args.tick_source = _tick_seconds_from_env()
    else:
        args.tick_source = "cli"
    return args


async def _amain(args: argparse.Namespace) -> SchedulerStats:
    engine = build_engine(get_settings())
    try:
        scheduler = Scheduler(
            session_factory=build_session_factory(engine),
            owner=args.owner,
            scope=args.scope,
            lease_seconds=args.lease_seconds,
            tick_seconds=args.tick_seconds,
            tick_source=args.tick_source,
            audit_verify=args.audit_verify,
            audit_image_scan=not args.no_audit_image_scan,
            audit_image_rows=args.audit_image_rows,
            audit_image_interval_seconds=args.audit_image_interval_seconds,
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
