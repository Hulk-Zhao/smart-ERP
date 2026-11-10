"""补偿执行器：独立常驻进程，消费 ``erp.compensation_task``（方案 B）。

启动方式（compose 直接拉起）：

    python -m apps.agents.compensation_worker

三条硬约束（设计提案 2026-11-11）：

1. discovery **只认 ``compensation_task`` 有到期行**，不按 ``ticket.state`` /
   ``ticket_step.state`` 扫描。API 的同步回滚（``RollbackService``）不回写
   ``ticket_step.state``，按状态扫会把已回滚的工单再补一遍；安全不能建立在
   外部系统的 ``:undo`` 幂等上。补偿行只在 ``TicketRuntime.compensate`` 里
   由「补偿中」工单入队，队列本身即事实来源。
2. 补偿执行期间续 ``compensation_task.heartbeat_at`` 与 ``erp.ticket`` 租约
   （见 ``TicketRuntime._compensate_with_heartbeat`` / ``_renew_ticket_lease``）。
   真实 HTTP 时延一旦超过 stale 阈值，行会被别的实例判死重认领。
3. 进程必须常驻，不提供 ``--once``。时钟回拨观测依赖本进程内
   ``monotonic()/wall_time()`` 成对采样，重启之间的回拨观测不到。

指标与健康检查由本进程自己 serve：``/metrics`` 与 ``/healthz``。只有「写指标
的进程自己暴露 /metrics」，规则才不是哑的。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.agents.compensation import CompensationQueue
from apps.agents.lease import LeaseManager, LeaseNotHeld
from apps.agents.runtime import (
    CompensateCall,
    CompensateResult,
    RuntimeGuardError,
    TicketRuntime,
)
from apps.agents.store import TicketStore
from apps.connectors.gateway import build_default_registry
from apps.platform.config import get_settings
from apps.platform.db.session import build_engine, build_session_factory
from apps.platform.policy.gateway import WriteGateway
from observability.metrics import MetricsRegistry, install_metrics

logger = logging.getLogger("erp.compensation_worker")

DEFAULT_PORT = 8013
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_STALE_SECONDS = 60.0
DEFAULT_LEASE_TTL_SECONDS = 30
DEFAULT_DISCOVERY_LIMIT = 20

#: 到期行：有 queued 且没有重试闸门（或闸门已到期）。
#: 唯一的发现口径是队列表本身——不 join ``erp.ticket`` 按状态扫，
#: 否则 API 同步回滚但未回写 ``ticket_step.state`` 的工单会被重复补偿。
_DISCOVER_SQL = text(
    """
    SELECT ct.ticket_id, min(ct.created_at) AS oldest
      FROM erp.compensation_task ct
     WHERE ct.state = 'queued'
       AND (ct.next_retry_at IS NULL OR ct.next_retry_at <= now())
     GROUP BY ct.ticket_id
     ORDER BY oldest
     LIMIT :limit
    """
)

#: 队列口径：backlog / 到期未领取 / 未来重试。
#: 主分支尚无 updated_at（0009 未合），到期判定用 coalesce(next_retry_at, created_at)，
#: 对 reclaim 后立刻再次到期的行会偏保守（偏大），作为告警阈值可接受。
_METRIC_SQL = text(
    """
    SELECT count(*) FILTER (WHERE state = 'queued') AS backlog,
           count(*) FILTER (
               WHERE state = 'queued'
                 AND (next_retry_at IS NULL OR next_retry_at <= now())
                 AND coalesce(next_retry_at, created_at)
                     <= now() - make_interval(secs => :overdue)
           ) AS overdue,
           count(*) FILTER (
               WHERE state = 'queued' AND next_retry_at > now() + make_interval(secs => :future)
           ) AS future_retry
      FROM erp.compensation_task
    """
)

_UNRESOLVED_SQL = text(
    """
    SELECT count(*) FROM erp.ticket
     WHERE state NOT IN ('已完成', '已回滚', '已终止', '已驳回')
    """
)


@dataclass(frozen=True, slots=True)
class _ConnectorCompCall:
    """连接器侧需要的补偿入参（``u8`` 读顶层 ``action_key`` / ``external_id``）。"""

    action_key: str
    payload: Mapping[str, Any]
    original_idem_key: str
    external_id: str | None


class RoutingConnector:
    """按 ``target_system`` 把补偿调用路由到进程内的具体连接器。"""

    def __init__(self, connectors: Mapping[str, Any]) -> None:
        self._by_system = {key: value for key, value in connectors.items() if key}

    async def compensate(self, call: CompensateCall, idem_key: str) -> CompensateResult:
        connector = self._by_system.get(call.target_system)
        if connector is None:
            raise RuntimeGuardError(
                f"没有可处理系统 {call.target_system!r} 的连接器（已注册："
                f"{sorted(self._by_system)}）"
            )
        return await connector.compensate(
            _ConnectorCompCall(
                action_key=call.action_key,
                payload=call.payload,
                original_idem_key=call.original_idem_key,
                external_id=call.external_id,
            ),
            idem_key=idem_key,
        )


async def _verifier_unavailable(planned: Any) -> Any:
    raise RuntimeGuardError("补偿执行器不执行前置写入校验（verify 属于 API 执行路径）")


@dataclass
class WorkerStatus:
    """``/healthz`` 读取的运行状态；全部来自本进程，不查库。"""

    started_at: float = field(default_factory=time.monotonic)
    last_tick_at: float | None = None
    ticks: int = 0
    executed: int = 0
    failures: int = 0
    last_error: str | None = None

    def snapshot(self, *, stale_after: float) -> dict[str, Any]:
        now = time.monotonic()
        healthy = (
            self.last_tick_at is not None and now - self.last_tick_at <= stale_after
        )
        return {
            "status": "ok" if healthy else "stale",
            "uptime_seconds": round(now - self.started_at, 3),
            "last_tick_age_seconds": (
                None if self.last_tick_at is None else round(now - self.last_tick_at, 3)
            ),
            "ticks": self.ticks,
            "executed": self.executed,
            "failures": self.failures,
            "last_error": self.last_error,
        }


class _ClockSampler:
    """进程内成对采样：单调钟前进 vs 墙钟前进，负 skew 即墙钟回拨。"""

    def __init__(self) -> None:
        self._monotonic = time.monotonic()
        self._wall = time.time()

    def sample(self, metrics: MetricsRegistry) -> None:
        monotonic_now = time.monotonic()
        wall_now = time.time()
        metrics.observe_clock_skew(
            monotonic_delta_seconds=max(0.0, monotonic_now - self._monotonic),
            wall_delta_seconds=wall_now - self._wall,
        )
        self._monotonic = monotonic_now
        self._wall = wall_now


class CompensationWorker:
    """单进程、顺序执行（一个工单处理完再下一个）；水平扩展由 DB 租约保证。"""

    def __init__(
        self,
        *,
        runtime: TicketRuntime,
        session_factory: async_sessionmaker[AsyncSession],
        metrics: MetricsRegistry,
        owner: str,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        discovery_limit: int = DEFAULT_DISCOVERY_LIMIT,
    ) -> None:
        self._runtime = runtime
        self._sf = session_factory
        self._metrics = metrics
        self._owner = owner
        self._poll = poll_seconds
        self._stale = stale_seconds
        self._limit = discovery_limit
        self._stop = asyncio.Event()
        self._clock = _ClockSampler()
        self.status = WorkerStatus()

    def stop(self) -> None:
        self._stop.set()

    def health_snapshot(self) -> dict[str, Any]:
        return self.status.snapshot(stale_after=max(30.0, self._poll * 5))

    async def run(self) -> None:
        logger.info(
            "补偿执行器启动 owner=%s poll=%.1fs stale=%.0fs limit=%d",
            self._owner,
            self._poll,
            self._stale,
            self._limit,
        )
        while not self._stop.is_set():
            await self.tick()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
        logger.info("补偿执行器停止 owner=%s ticks=%d", self._owner, self.status.ticks)

    async def tick(self) -> None:
        self.status.ticks += 1
        try:
            await self._runtime.recover_stale_compensations(
                stale_after_seconds=self._stale
            )
        except Exception as exc:
            logger.exception("回收 stale 补偿任务失败")
            self._record_error(exc)
        try:
            await self._refresh_metrics()
        except Exception as exc:
            logger.exception("刷新补偿指标失败")
            self._record_error(exc)

        try:
            ticket_ids = await self._discover()
        except Exception as exc:  # 数据库抖动不应打死常驻进程
            logger.exception("发现待补偿工单失败")
            self._record_error(exc)
            ticket_ids = []

        for ticket_id in ticket_ids:
            if self._stop.is_set():
                break
            try:
                await self._runtime.compensate(ticket_id, owner=self._owner)
                self.status.executed += 1
            except LeaseNotHeld as exc:
                logger.info("工单 %s 补偿租约被占用，跳过：%s", ticket_id, exc)
            except Exception as exc:
                logger.exception("工单 %s 补偿执行失败", ticket_id)
                self._record_error(exc)
        self.status.last_tick_at = time.monotonic()

    def _record_error(self, exc: Exception) -> None:
        self.status.failures += 1
        self.status.last_error = f"{type(exc).__name__}: {exc}"

    async def _discover(self) -> list[Any]:
        async with self._sf() as session:
            rows = (await session.execute(_DISCOVER_SQL, {"limit": self._limit})).all()
        return [row.ticket_id for row in rows]

    async def _refresh_metrics(self) -> None:
        async with self._sf() as session:
            queue = (
                await session.execute(
                    _METRIC_SQL, {"overdue": self._stale, "future": 18.0}
                )
            ).one()
            unresolved = (await session.execute(_UNRESOLVED_SQL)).scalar_one()
        self._metrics.set_compensation_backlog(int(queue.backlog))
        self._metrics.set_compensation_overdue(int(queue.overdue))
        self._metrics.set_compensation_future_retry(int(queue.future_retry))
        self._metrics.set_unresolved(int(unresolved))
        self._clock.sample(self._metrics)


def build_http_app(worker: CompensationWorker, metrics: MetricsRegistry) -> Any:
    """``/metrics`` 与 ``/healthz``；独立内网端口，不复用 API 的暴露面。"""

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="smart-erp compensation worker", docs_url=None, redoc_url=None)
    install_metrics(app, metrics, enabled=True)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        snapshot = worker.health_snapshot()
        return JSONResponse(
            snapshot, status_code=200 if snapshot["status"] == "ok" else 503
        )

    return app


def build_owner(explicit: str | None) -> str:
    """默认 owner = 容器 hostname:pid，容器外可直接核对租约持有者。"""

    if explicit:
        return explicit
    host = socket.gethostname() or "unknown"
    return f"{host}:{os.getpid()}"


def build_router() -> tuple[RoutingConnector, dict[str, Any]]:
    registry = build_default_registry()
    connectors: dict[str, Any] = {}
    for alias in registry.aliases:
        connector = registry.connector_for(alias)
        connectors[alias] = connector
        system = getattr(getattr(connector, "meta", None), "system", None)
        if system:
            connectors[str(system)] = connector
    return RoutingConnector(connectors), connectors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="smart-erp 补偿执行器（常驻）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ERP_COMPENSATION_PORT", DEFAULT_PORT)),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(
            os.environ.get("ERP_COMPENSATION_POLL_SECONDS", DEFAULT_POLL_SECONDS)
        ),
    )
    parser.add_argument(
        "--stale-seconds",
        type=float,
        default=float(
            os.environ.get("ERP_COMPENSATION_STALE_SECONDS", DEFAULT_STALE_SECONDS)
        ),
    )
    parser.add_argument(
        "--lease-ttl-seconds", type=int, default=DEFAULT_LEASE_TTL_SECONDS
    )
    parser.add_argument("--discovery-limit", type=int, default=DEFAULT_DISCOVERY_LIMIT)
    parser.add_argument("--owner", default=None)
    return parser.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    import uvicorn

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    metrics = MetricsRegistry()
    router, connectors = build_router()
    owner = build_owner(args.owner)
    logger.info("已装配连接器：%s", sorted(connectors))

    runtime = TicketRuntime(
        store=TicketStore(session_factory),
        leases=LeaseManager(session_factory),
        gateway=WriteGateway(router),  # 补偿路径不走网关；保留构造约束
        compensation=CompensationQueue(session_factory),
        verifier=_verifier_unavailable,
        compensator=router.compensate,
        lease_ttl_seconds=args.lease_ttl_seconds,
        compensation_stale_seconds=args.stale_seconds,
    )
    worker = CompensationWorker(
        runtime=runtime,
        session_factory=session_factory,
        metrics=metrics,
        owner=owner,
        poll_seconds=args.poll_seconds,
        stale_seconds=args.stale_seconds,
        discovery_limit=args.discovery_limit,
    )
    app = build_http_app(worker, metrics)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)

    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    )
    server_task = asyncio.create_task(server.serve())
    try:
        await worker.run()
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server_task, timeout=10)
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args(argv)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:  # Windows 下 add_signal_handler 不可用
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
