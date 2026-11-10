"""U8 连接器装配：限流 / 熔断 / 幂等 / 传输 / 连接器 / 写入闸门一次装好。

装配只在这里做，测试与 gateway 复用同一份逻辑——避免「测试通过但生产接线不同」。
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from apps.connectors.breaker import BreakerPolicy, CircuitBreaker
from apps.connectors.canonical.mapping import MappingContext
from apps.connectors.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from apps.connectors.ratelimit import ActionRateLimiter
from apps.connectors.retry import RetryPolicy
from apps.connectors.runtime import ResilientCaller
from apps.connectors.u8.actions import ACTIONS, READ_LIMIT_KEY
from apps.connectors.u8.connector import U8Connector
from apps.connectors.u8.transport import U8Transport
from apps.platform.policy.gateway import ApprovalGate, DenyAllApprovals, WriteGateway
from apps.platform.policy.levels import RateLimit, WriteLevel

DEFAULT_BASE_URL = "http://u8.invalid"


@dataclass
class U8Stack:
    connector: U8Connector
    transport: U8Transport
    gateway: WriteGateway
    limiter: ActionRateLimiter
    breaker: CircuitBreaker
    idem: IdempotencyStore
    client: httpx.AsyncClient


def build_u8_stack(
    *,
    client: httpx.AsyncClient | None = None,
    ctx: MappingContext | None = None,
    experimental: bool = False,
    retry_policy: RetryPolicy | None = None,
    timeout_s: float = 30.0,
    read_limit: RateLimit | None = None,
    breaker_policy: BreakerPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
    idem: IdempotencyStore | None = None,
    approvals: ApprovalGate | None = None,
    base_url: str | None = None,
) -> U8Stack:
    ctx = ctx or MappingContext(connector="u8", authority_system="erp")
    http_client = client or httpx.AsyncClient(
        base_url=base_url or os.environ.get("ERP_U8_BASE_URL", DEFAULT_BASE_URL),
        timeout=timeout_s,
    )

    limits: dict[str, RateLimit] = {
        READ_LIMIT_KEY: read_limit or RateLimit(qps=5.0, daily_quota=5_000, concurrency=4),
        **{action.key: action.rate_limit for action in ACTIONS},
    }
    limiter = ActionRateLimiter(limits)
    breaker = CircuitBreaker("u8", policy=breaker_policy)
    caller = ResilientCaller(
        limiter=limiter,
        breaker=breaker,
        retry_policy=retry_policy,
        timeout_s=timeout_s,
        sleep=sleep,
        rng=rng,
    )
    store = idem or InMemoryIdempotencyStore()
    transport = U8Transport(client=http_client, caller=caller, idem=store, ctx=ctx)
    connector = U8Connector(transport, ctx=ctx, experimental=experimental)
    gateway = WriteGateway(
        connector,
        connector_cap=lambda _key: _connector_cap(connector),
        breaker_cap=lambda _scene: connector.breaker.write_cap(),
        approvals=approvals or DenyAllApprovals(),
    )
    return U8Stack(
        connector=connector,
        transport=transport,
        gateway=gateway,
        limiter=limiter,
        breaker=breaker,
        idem=store,
        client=http_client,
    )


def _connector_cap(connector: U8Connector) -> WriteLevel:
    return connector.connector_cap()


class AllowAllApprovals:
    """仅用于测试/演示的审批门；生产用 S3 的工单审批实现。"""

    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        return True
