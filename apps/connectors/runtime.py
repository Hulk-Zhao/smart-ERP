"""韧性调用壳：熔断 → 限流/并发 → 超时 → 重试 → 错误分类。

顺序是设计的一部分，不能换：

1. **熔断最先**：打开期间一次请求都不发（FR-1107「写入 0 次」）；
2. 限流在超时之前：拿不到令牌属于「排队」，不应被算作调用超时；
3. 每次重试都重新过限流与并发配额（重试也是真实请求）；
4. ``unknown_outcome`` 不重试，由调用方走 verify。

连接器与测试只通过本类发请求，避免每个连接器各写一套重试逻辑。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from apps.connectors.breaker import CircuitBreaker
from apps.connectors.errors import ConnectorError, ErrorKind, classify_exception
from apps.connectors.ratelimit import ActionRateLimiter
from apps.connectors.retry import RetryHook, RetryPolicy, retry_async

T = TypeVar("T")


class ResilientCaller:
    def __init__(
        self,
        *,
        limiter: ActionRateLimiter,
        breaker: CircuitBreaker,
        retry_policy: RetryPolicy | None = None,
        timeout_s: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        on_retry: RetryHook | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("单次调用超时必须为正（§3.5.6 基线 30s）")
        self._limiter = limiter
        self._breaker = breaker
        self._retry_policy = retry_policy or RetryPolicy()
        self._timeout_s = timeout_s
        self._sleep = sleep
        self._rng = rng
        self._on_retry = on_retry

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def limiter(self) -> ActionRateLimiter:
        return self._limiter

    async def call(
        self,
        action_key: str,
        operation: Callable[[], Awaitable[T]],
        *,
        phase: str = "read",
    ) -> T:
        self._breaker.require_closed()

        async def attempt() -> T:
            async with self._limiter.slot(action_key):
                try:
                    return await asyncio.wait_for(operation(), timeout=self._timeout_s)
                except TimeoutError:
                    raise classify_exception(TimeoutError(), phase=phase) from None

        try:
            result = await retry_async(
                attempt,
                policy=self._retry_policy,
                sleep=self._sleep,
                rng=self._rng,
                on_retry=self._on_retry,
            )
        except ConnectorError as err:
            if err.kind is not ErrorKind.CIRCUIT_OPEN:
                self._breaker.record_failure()
            raise
        except Exception as exc:  # 未分类异常也要计数，否则熔断永远不触发
            self._breaker.record_failure()
            raise classify_exception(exc, phase=phase) from exc
        self._breaker.record_success()
        return result
