"""文件通道装配：把限流、熔断、重试、幂等与私有 transport 接成一条链。

``root`` 解析顺序：显式参数 → ``ERP_FILE_INBOX`` 环境变量 → ``var/inbox``。
目录可以暂时不存在（连接器构造不读盘），首次读会以 ``not_found`` 退化返回。
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from apps.connectors.breaker import BreakerPolicy, CircuitBreaker
from apps.connectors.canonical.mapping import MappingContext
from apps.connectors.file.connector import FileConnector
from apps.connectors.file.spec import DEFAULT_READ_LIMIT, READ_LIMIT_KEY, SYSTEM_NAME
from apps.connectors.file.transport import FileTransport
from apps.connectors.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from apps.connectors.ratelimit import ActionRateLimiter
from apps.connectors.retry import RetryHook, RetryPolicy
from apps.connectors.runtime import ResilientCaller
from apps.platform.policy.levels import RateLimit


@dataclass(frozen=True, slots=True)
class FileStack:
    connector: FileConnector
    transport: FileTransport
    limiter: ActionRateLimiter
    breaker: CircuitBreaker
    idem: IdempotencyStore
    root: Path


def build_file_stack(
    *,
    root: str | Path | None = None,
    ctx: MappingContext | None = None,
    read_limit: RateLimit | None = None,
    retry_policy: RetryPolicy | None = None,
    breaker_policy: BreakerPolicy | None = None,
    timeout_s: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
    on_retry: RetryHook | None = None,
    idem: IdempotencyStore | None = None,
    limiter: ActionRateLimiter | None = None,
) -> FileStack:
    resolved_root = (
        Path(root)
        if root is not None
        else Path(os.environ.get("ERP_FILE_INBOX") or "var/inbox")
    )
    limit = read_limit or DEFAULT_READ_LIMIT
    rate_limiter = limiter or ActionRateLimiter({READ_LIMIT_KEY: limit})
    breaker = CircuitBreaker(SYSTEM_NAME, policy=breaker_policy)
    caller = ResilientCaller(
        limiter=rate_limiter,
        breaker=breaker,
        retry_policy=retry_policy,
        timeout_s=timeout_s,
        sleep=sleep,
        rng=rng,
        on_retry=on_retry,
    )
    store = idem or InMemoryIdempotencyStore()
    transport = FileTransport(root=resolved_root, caller=caller, idem=store)
    connector = FileConnector(transport, ctx=ctx, rate_limit=limit)
    return FileStack(
        connector=connector,
        transport=transport,
        limiter=rate_limiter,
        breaker=breaker,
        idem=store,
        root=resolved_root,
    )
