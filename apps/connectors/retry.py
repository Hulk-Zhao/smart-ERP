"""重试与退避（FR-1104 / 技术架构 §3.5.5）。

只在 ``ErrorClass.TRANSIENT`` 上重试；``unknown_outcome`` 必须先 verify 再决定，
所以这里**绝不**对未知结果重试（否则同一张单据可能被创建两次）。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from apps.connectors.errors import ConnectorError

T = TypeVar("T")

Sleep = Callable[[float], Awaitable[None]]
RetryHook = Callable[[int, ConnectorError, float], None]

MAX_RETRY_AFTER_S = 60.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """默认 3 次、0.5s 起、上限 8s、25% 向上抖动。"""

    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if self.base_delay_s <= 0 or self.max_delay_s <= 0:
            raise ValueError("退避时间必须为正")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio 必须在 0..1")


def backoff_delay(
    attempt: int,
    policy: RetryPolicy,
    *,
    rng: random.Random | None = None,
) -> float:
    """第 ``attempt`` 次失败后的等待秒数（attempt 从 1 开始）。

    指数底数 2，向上抖动：结果落在 ``[raw, raw * (1 + jitter_ratio)]``，
    保证任何一次等待都不早于名义退避（对目标系统更保守）。
    """

    if attempt < 1:
        raise ValueError("attempt 从 1 开始")
    raw = min(policy.base_delay_s * (2 ** (attempt - 1)), policy.max_delay_s)
    if policy.jitter_ratio == 0:
        return raw
    generator = rng or random.Random()
    return raw + generator.random() * raw * policy.jitter_ratio


def delay_for(err: ConnectorError, attempt: int, policy: RetryPolicy, *, rng=None) -> float:
    """限流响应里的 ``Retry-After`` 优先于本地退避（服务端更清楚何时恢复）。"""

    if err.retry_after_s is not None:
        return min(max(err.retry_after_s, 0.0), MAX_RETRY_AFTER_S)
    return backoff_delay(attempt, policy, rng=rng)


async def retry_async(
    call: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    on_retry: RetryHook | None = None,
) -> T:
    """执行 ``call``，按策略重试；非重试类错误立即抛出。"""

    policy = policy or RetryPolicy()
    attempt = 0
    while True:
        attempt += 1
        try:
            return await call()
        except ConnectorError as err:
            if not err.retryable or attempt >= policy.max_attempts:
                raise
            # 服务端要求等待超过一分钟（如日配额耗尽）时不在请求路径里硬等，
            # 直接抛给运行时去排队/降并发。
            if err.retry_after_s is not None and err.retry_after_s > MAX_RETRY_AFTER_S:
                raise
            wait = delay_for(err, attempt, policy, rng=rng)
            if on_retry is not None:
                on_retry(attempt, err, wait)
            await sleep(wait)
