"""限流（FR-109 / FR-1205 / 技术架构 §3.5.5）。

三种预算分别管不同的事，缺一不可：

- **QPS**：令牌桶，限制瞬时冲击（目标 ERP 最容易被打挂的地方）；
- **日配额**：批量发起前就要能算「今天跑不跑得完」（NFR-P-02 的前提）；
- **并发**：与运行时的线程/协程配额一致，余量 < 20% 时按比例降并发。

实现可替换：接口是 ``RateLimiter``，生产实现落 PG ``rate_bucket``（可重建数据），
本文件的 ``TokenBucket``/``ActionRateLimiter`` 是单进程实现，gateway 与测试共用。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from apps.connectors.errors import ConnectorError, ErrorKind
from apps.platform.policy.levels import RateLimit

Clock = Callable[[], float]


class RateLimiter(Protocol):
    async def acquire(self, key: str, tokens: float = 1.0) -> None: ...

    def remaining_ratio(self, key: str) -> float: ...


class TokenBucket:
    """单键令牌桶。``clock`` / ``sleep`` 可注入，测试里不真的等待。"""

    def __init__(
        self,
        *,
        qps: float,
        burst: float | None = None,
        clock: Clock = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        if qps <= 0:
            raise ValueError("qps 必须为正")
        self.qps = qps
        self.capacity = burst if burst is not None else max(1.0, qps)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.qps)

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def remaining_ratio(self) -> float:
        return max(0.0, min(1.0, self.tokens / self.capacity))

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """非阻塞：拿不到就立刻返回 False（熔断巡检/降并发判定用）。"""

        if tokens <= 0:
            return True
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> float:
        """阻塞直到拿到令牌；返回累计等待秒数（可观测）。

        等待在锁内进行：同一进程内同一动作的调用被串行排队，不会一起越过 QPS。
        跨进程的真实配额由 PG ``rate_bucket`` 兜底（本类是单进程实现）。
        """

        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                delay = (tokens - self._tokens) / self.qps
                waited += delay
                await self._sleep(delay)


@dataclass
class _DailyQuota:
    limit: int
    used: int = 0
    day: date = field(default_factory=lambda: datetime.now(UTC).date())

    def _roll(self) -> None:
        today = datetime.now(UTC).date()
        if today != self.day:
            self.day = today
            self.used = 0

    def remaining(self) -> int:
        self._roll()
        return max(0, self.limit - self.used)

    def consume(self, count: int = 1) -> bool:
        self._roll()
        if self.used + count > self.limit:
            return False
        self.used += count
        return True

    def ratio(self) -> float:
        self._roll()
        return 0.0 if self.limit <= 0 else max(0.0, min(1.0, self.remaining() / self.limit))


class ActionRateLimiter:
    """按动作键聚合的限流器：QPS + 日配额 + 并发上限。

    并发用 ``asyncio.Semaphore``，与运行时「每连接器独立配额」一致（§1.3）。
    """

    def __init__(
        self,
        limits: Mapping[str, RateLimit],
        *,
        clock: Clock = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self._limits = dict(limits)
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._quotas: dict[str, _DailyQuota] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def _limit(self, key: str) -> RateLimit:
        limit = self._limits.get(key)
        if limit is None:
            raise KeyError(f"动作 {key} 未声明限流预算（C2 要求每个动作都声明）")
        return limit

    def _bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            limit = self._limit(key)
            self._buckets[key] = TokenBucket(
                qps=limit.qps, burst=max(1.0, limit.qps), clock=self._clock, sleep=self._sleep
            )
            self._quotas[key] = _DailyQuota(limit=limit.daily_quota)
            self._semaphores[key] = asyncio.Semaphore(max(1, limit.concurrency))
        return self._buckets[key]

    def remaining_ratio(self, key: str) -> float:
        """QPS 余量与日配额余量取更紧的一个（FR-1205 的「限流余量」）。"""

        self._bucket(key)
        return min(self._bucket(key).remaining_ratio(), self._quotas[key].ratio())

    def daily_remaining(self, key: str) -> int:
        self._bucket(key)
        return self._quotas[key].remaining()

    def suggested_concurrency(self, key: str, base: int | None = None) -> int:
        """余量 < 20% 时按余量比例降并发（FR-1205 验收「降并发自动生效」）。"""

        limit = self._limit(key)
        base = base or limit.concurrency
        ratio = self.remaining_ratio(key)
        if ratio >= 0.2:
            return base
        return max(1, int(base * ratio / 0.2))

    def reserve_budget(self, key: str, count: int) -> bool:
        """批量发起前预留日配额；返回 False 表示今天跑不完，调用方必须明确失败。"""

        self._bucket(key)
        return self._quotas[key].consume(count)

    async def acquire(self, key: str, tokens: float = 1.0) -> None:
        bucket = self._bucket(key)
        if not self._quotas[key].consume(1):
            raise ConnectorError(
                ErrorKind.RATE_LIMITED,
                f"动作 {key} 超出日配额 {self._limit(key).daily_quota}",
                retry_after_s=3600.0,
            )
        await bucket.acquire(tokens)

    @asynccontextmanager
    async def slot(self, key: str) -> AsyncIterator[None]:
        """一个受并发与限流约束的调用位。异常不吞，配额不退回（保守）。"""

        semaphore = self._semaphore(key)
        await semaphore.acquire()
        try:
            await self.acquire(key)
            yield
        finally:
            semaphore.release()

    def _semaphore(self, key: str) -> asyncio.Semaphore:
        self._bucket(key)
        return self._semaphores[key]
