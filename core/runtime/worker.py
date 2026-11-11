"""C15 worker：消费 ``erp.job_queue``，心跳落库，SIGTERM 优雅退出。

启动方式（compose 直接拉起）：

    python -m core.runtime.worker --queues agent,io --concurrency 4

关键语义：

- 认领：``FOR UPDATE SKIP LOCKED``（见 :mod:`core.runtime.queue`），单跑由
  ``state='running'`` + 租约保证；
- 心跳：每 ``--heartbeat-seconds`` upsert 一行 ``erp.worker_heartbeat``，
  健康探针（``python -m tools.health worker``）只读这张表；
- 回收：启动时与每次心跳顺手回收 ``lease_expires_at < now()`` 的 running，
  上一个 worker 被 kill 后任务会重新可认领，不依赖进程内状态；
- 失败：未到 ``max_attempts`` 回队列并退避，到上限转 ``dead``（终态）；
  未知 kind 直接 ``dead``，避免永远重试；
- 退出：收到 SIGTERM/SIGINT 后停止认领，等在跑的任务完成（``--drain-seconds``），
  最后写一次心跳（``state='stopped'``）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.platform.config import get_settings
from apps.platform.db.session import build_engine, build_session_factory

from core.runtime.queue import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    Job,
    JobQueueStore,
)

logger = logging.getLogger("erp.worker")

JobHandler = Callable[[Job], Awaitable[None]]

# 常驻进程默认值：心跳必须显著小于租约 TTL，否则机器稍慢就会被误判为死亡、
# 任务被第二个 worker 抢跑。默认 10s 心跳 / 60s 租约 = 6 个心跳周期。
DEFAULT_CONCURRENCY = 4
DEFAULT_HEARTBEAT_SECONDS = 10.0
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_DRAIN_SECONDS = 30.0

# 任务租约每次续约的间隔是 lease/3（_run_job 的 asyncio.wait 超时）；
# 心跳间隔不得超过租约的一半——再稀疏就会让 stale 回收与存活信号滞后一整个租约窗口。
HEARTBEAT_RENEW_DIVISOR = 3
HEARTBEAT_MAX_LEASE_RATIO = 0.5
_HEARTBEAT_ENV = "ERP_HEARTBEAT_INTERVAL_SECONDS"


def _heartbeat_seconds_from_env() -> tuple[float, str]:
    """未给 --heartbeat-seconds 时读环境变量，返回 (秒数, 来源)。

    空值回落默认 10s；非数字或 ≤0 直接抛 SystemExit 并带上键名，不静默回落——
    compose 里写着一个看似生效的值而实际不生效，比没有这个变量更危险。
    """
    raw = os.environ.get(_HEARTBEAT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_HEARTBEAT_SECONDS, "default"
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(
            f"{_HEARTBEAT_ENV}={raw!r} 不是数字；留空或删除该变量则使用默认 "
            f"{DEFAULT_HEARTBEAT_SECONDS}s"
        ) from None
    if value <= 0:
        raise SystemExit(f"{_HEARTBEAT_ENV}={raw!r} 必须 > 0")
    return value, "env"


HANDLERS: dict[str, JobHandler] = {}


class UnknownJobKind(Exception):
    """没有注册处理函数：重试也不会好，直接死信。"""


def register_handler(kind: str) -> Callable[[JobHandler], JobHandler]:
    def decorator(func: JobHandler) -> JobHandler:
        HANDLERS[kind] = func
        return func

    return decorator


@register_handler("echo")
async def _handle_echo(job: Job) -> None:
    """空操作：用于连通性/复验冒烟。"""


@register_handler("sleep")
async def _handle_sleep(job: Job) -> None:
    """按 payload.seconds 睡一会儿：用于 kill -9 回收演练。"""

    await asyncio.sleep(float(job.payload.get("seconds", 0)))


@dataclass
class WorkerStats:
    done: int = 0
    failed: int = 0


@dataclass
class Worker:
    session_factory: async_sessionmaker[AsyncSession]
    queues: Sequence[str]
    worker_id: str
    concurrency: int = DEFAULT_CONCURRENCY
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    # 心跳的来源（cli/env/default），只用于启动日志对账，不参与调度决策。
    heartbeat_source: str = "default"
    poll_seconds: float = DEFAULT_POLL_SECONDS
    drain_seconds: float = DEFAULT_DRAIN_SECONDS
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    handlers: dict[str, JobHandler] = field(default_factory=lambda: dict(HANDLERS))
    queue: JobQueueStore = field(init=False)
    stats: WorkerStats = field(default_factory=WorkerStats)
    _in_flight: int = 0
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency 必须 >= 1")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须 > 0")
        # 心跳间隔必须明显小于租约 TTL：否则心跳还没写下一次，租约先过期，
        # 正常运行中的任务会被别的 worker 当成 stale 抢走（双跑）。
        if self.heartbeat_seconds <= 0:
            raise ValueError(
                f"heartbeat_seconds={self.heartbeat_seconds} 必须 > 0"
                f"（来自 {_HEARTBEAT_ENV} 或 --heartbeat-seconds）"
            )
        limit = self.lease_seconds * HEARTBEAT_MAX_LEASE_RATIO
        if self.heartbeat_seconds > limit:
            raise ValueError(
                f"heartbeat_seconds={self.heartbeat_seconds} 超过上限 {limit}："
                f"上限 = lease_seconds({self.lease_seconds}) × "
                f"{HEARTBEAT_MAX_LEASE_RATIO}（租约的一半）；"
                f"heartbeat 来自 {_HEARTBEAT_ENV} 或 --heartbeat-seconds，"
                "lease 来自 --lease-seconds。"
                "注意：调小租约默认值也会让原本合法的 heartbeat 变非法，"
                "不是心跳配置写错了。"
                "心跳比租约还稀疏时，stale 回收和存活信号会滞后一整个租约窗口。"
            )
        self.queue = JobQueueStore(self.session_factory)

    @property
    def heartbeat_limit(self) -> float:
        """心跳间隔的硬上限（租约的一半），与启动日志同一个口径。"""
        return self.lease_seconds * HEARTBEAT_MAX_LEASE_RATIO

    @property
    def lease_renew_seconds(self) -> float:
        """任务租约的续约周期（lease/3），与 _run_job 的等待超时一致。"""
        return self.lease_seconds / HEARTBEAT_RENEW_DIVISOR

    def _log_startup(self) -> None:
        """启动日志：配置值 / 来源 / 租约 / 续约周期 / 心跳上限，一次说全。

        注释会腐烂，这行日志不会——「我把 30 改成了 300 为什么起不来」
        的答案必须在 docker logs 里。
        """
        logger.info(
            "worker %s 启动 queues=%s concurrency=%s heartbeat_seconds=%s source=%s "
            "lease=%ss renew=%ss heartbeat_limit=%ss (lease/2)",
            self.worker_id,
            ",".join(self.queues),
            self.concurrency,
            self.heartbeat_seconds,
            self.heartbeat_source,
            self.lease_seconds,
            self.lease_renew_seconds,
            self.heartbeat_limit,
        )

    # ---------------------------------------------------------------- 心跳
    async def _beat(self, state: str) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO erp.worker_heartbeat "
                    "(worker_id, queues, state, heartbeat_at, jobs_done, jobs_failed) "
                    "VALUES (:worker_id, :queues, :state, now(), :done, :failed) "
                    "ON CONFLICT (worker_id) DO UPDATE SET "
                    "queues = EXCLUDED.queues, state = EXCLUDED.state, "
                    "heartbeat_at = now(), jobs_done = EXCLUDED.jobs_done, "
                    "jobs_failed = EXCLUDED.jobs_failed"
                ),
                {
                    "worker_id": self.worker_id,
                    "queues": list(self.queues),
                    "state": state,
                    "done": self.stats.done,
                    "failed": self.stats.failed,
                },
            )

    async def _describe_lease_loss(self, job_id: int) -> str:
        """租约落账失败时读回库里的真实状态，说明实际值以及任务会不会重跑。"""

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT state, lease_owner FROM erp.job_queue WHERE id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            ).mappings().one_or_none()
        if row is None:
            return "实际：库中已无此行；任务不会重跑，本次结果丢失"
        state = str(row["state"])
        actual_owner = row["lease_owner"]
        if state == "queued":
            fate = "任务已回 queued，会被重新领取再跑一遍（at-least-once，handler 必须幂等）"
        elif state == "running":
            fate = (
                f"任务已被 owner={actual_owner} 重新持有，会由它再跑一遍"
                "（at-least-once，handler 必须幂等）"
            )
        elif state == "dead":
            fate = "任务已转 dead（attempts 用尽），不会自动重跑，本次结果永久丢失，需人工重放"
        else:
            fate = f"任务已是终态 {state}，不会重跑，本次结果被丢弃"
        return f"实际 state={state} lease_owner={actual_owner}；{fate}"

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._beat("busy" if self._in_flight else "idle")
                reclaimed = await self.queue.reclaim_stale(
                    exclude_owner=self.worker_id,
                    grace_seconds=self.lease_renew_seconds,
                )
                if reclaimed:
                    logger.warning("回收 stale running 任务 %s 条", reclaimed)
            except Exception:  # noqa: BLE001 - 心跳失败不拖死消费主循环
                logger.exception("写心跳失败")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.heartbeat_seconds
                )
            except asyncio.TimeoutError:
                continue

    # ---------------------------------------------------------------- 消费
    async def _run_job(self, job: Job) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            self.stats.failed += 1
            state = await self.queue.fail(
                job.id,
                owner=self.worker_id,
                error=f"未知任务类型: {job.kind}",
                permanent=True,
            )
            logger.error("任务 %s kind=%s 未知，转 %s", job.id, job.kind, state)
            return
        job_task = asyncio.ensure_future(handler(job))
        while True:
            done, _ = await asyncio.wait({job_task}, timeout=self.lease_seconds / 3)
            if done:
                break
            # 长任务续租，避免执行中被别的 worker 判 stale 抢走
            renewed = await self.queue.heartbeat_lease(
                job.id, owner=self.worker_id, lease_seconds=self.lease_seconds
            )
            if not renewed:
                detail = await self._describe_lease_loss(job.id)
                logger.warning(
                    "任务 %s 续租失败：租约已被他人接管，本次结果被丢弃"
                    "（到 complete 时不会再落 succeeded）。"
                    "预期 state=running lease_owner=%s；%s",
                    job.id,
                    self.worker_id,
                    detail,
                )
        try:
            job_task.result()
        except asyncio.CancelledError:
            self.stats.failed += 1
            await self.queue.fail(
                job.id, owner=self.worker_id, error="worker 停止，任务回队列"
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 失败落账后继续下一条
            self.stats.failed += 1
            state = await self.queue.fail(
                job.id,
                owner=self.worker_id,
                error=f"{type(exc).__name__}: {exc}",
                backoff_seconds=self.retry_backoff_seconds,
            )
            logger.warning("任务 %s 失败 attempts=%s -> %s", job.id, job.attempts, state)
        else:
            completed = await self.queue.complete(job.id, owner=self.worker_id)
            if not completed:
                detail = await self._describe_lease_loss(job.id)
                logger.warning(
                    "任务 %s 完成落账失败：租约已被他人接管，本次结果被丢弃。"
                    "预期 state=running lease_owner=%s；%s",
                    job.id,
                    self.worker_id,
                    detail,
                )
            self.stats.done += 1

    async def _consume(self, max_jobs: int | None, processed: list[int]) -> None:
        while not self._stop.is_set():
            if max_jobs is not None and processed[0] >= max_jobs:
                self._stop.set()
                return
            job = await self.queue.claim(
                self.queues, owner=self.worker_id, lease_seconds=self.lease_seconds
            )
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            self._in_flight += 1
            try:
                await self._run_job(job)
            finally:
                self._in_flight -= 1
                processed[0] += 1

    # ---------------------------------------------------------------- 生命周期
    def stop(self) -> None:
        self._stop.set()

    async def run(self, *, max_jobs: int | None = None, install_signals: bool = False) -> WorkerStats:
        loop = asyncio.get_running_loop()
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, self.stop)
                except NotImplementedError:  # pragma: no cover - Windows 事件循环
                    signal.signal(sig, lambda *_: self.stop())

        await self._beat("starting")
        reclaimed = await self.queue.reclaim_stale(
            exclude_owner=self.worker_id,
            grace_seconds=self.lease_renew_seconds,
        )
        if reclaimed:
            logger.warning("启动回收 stale running 任务 %s 条", reclaimed)
        self._log_startup()

        processed = [0]
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        consumers = [
            asyncio.create_task(self._consume(max_jobs, processed))
            for _ in range(max(1, self.concurrency))
        ]
        gather = asyncio.gather(*consumers)
        stop_wait = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait(
                {gather, stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if not gather.done():
                # 收到停止信号：停止认领，给在跑任务 drain_seconds 完成
                try:
                    await asyncio.wait_for(gather, timeout=self.drain_seconds)
                except asyncio.TimeoutError:
                    logger.warning(
                        "drain 超时 %ss，取消在跑任务（租约到期后可被回收）",
                        self.drain_seconds,
                    )
                    for consumer in consumers:
                        consumer.cancel()
                    await asyncio.gather(*consumers, return_exceptions=True)
            else:
                await gather
        finally:
            stop_wait.cancel()
            self._stop.set()
            heartbeat.cancel()
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            await self._beat("stopped")
        logger.info(
            "worker %s 退出 done=%s failed=%s", self.worker_id, self.stats.done, self.stats.failed
        )
        return self.stats


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="C15 worker：消费 erp.job_queue")
    parser.add_argument("--queues", default="agent", help="逗号分隔，例如 agent,io")
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY
    )
    parser.add_argument(
        "--worker-id", default=f"{socket.gethostname()}:{os.getpid()}"
    )
    parser.add_argument("--lease-seconds", type=float, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=None,
        help=(
            f"心跳间隔秒数；未指定时依次取 {_HEARTBEAT_ENV}、默认 "
            f"{DEFAULT_HEARTBEAT_SECONDS}s（必须 ≤ lease/2）"
        ),
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--drain-seconds", type=float, default=DEFAULT_DRAIN_SECONDS)
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
        help="失败后回队列的退避秒数",
    )
    parser.add_argument(
        "--max-jobs", type=int, default=None, help="处理 N 条后退出（冒烟/测试用）"
    )
    args = parser.parse_args(argv)
    if args.heartbeat_seconds is None:
        args.heartbeat_seconds, args.heartbeat_source = (
            _heartbeat_seconds_from_env()
        )
    else:
        args.heartbeat_source = "cli"
    return args


async def _amain(args: argparse.Namespace) -> WorkerStats:
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        worker = Worker(
            session_factory=session_factory,
            queues=[item.strip() for item in args.queues.split(",") if item.strip()],
            worker_id=args.worker_id,
            concurrency=args.concurrency,
            lease_seconds=args.lease_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            heartbeat_source=args.heartbeat_source,
            poll_seconds=args.poll_seconds,
            drain_seconds=args.drain_seconds,
            retry_backoff_seconds=args.retry_backoff_seconds,
        )
        return await worker.run(max_jobs=args.max_jobs, install_signals=True)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        level=os.environ.get("ERP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
