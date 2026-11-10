"""限流：令牌桶、日配额预留、并发降级、超配额报错。"""

from __future__ import annotations

import pytest

from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.ratelimit import ActionRateLimiter, TokenBucket
from apps.platform.policy.levels import RateLimit
from tests.connectors.support import FakeClock, run


def test_token_bucket_allows_burst_then_blocks() -> None:
    clock = FakeClock()
    bucket = TokenBucket(qps=1.0, burst=2.0, clock=clock, sleep=clock.sleep)

    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    # 桶空了：非阻塞拿不到
    assert bucket.try_acquire() is False


def test_token_bucket_waits_for_refill() -> None:
    clock = FakeClock()
    bucket = TokenBucket(qps=2.0, burst=1.0, clock=clock, sleep=clock.sleep)

    assert bucket.try_acquire() is True
    waited = run(bucket.acquire())
    # 2 qps → 缺 1 个令牌要等 0.5s
    assert waited == pytest.approx(0.5)
    assert clock.sleeps == [pytest.approx(0.5)]


def test_remaining_ratio_and_daily_quota() -> None:
    clock = FakeClock()
    limiter = ActionRateLimiter(
        {"u8.po.create_draft": RateLimit(qps=10.0, daily_quota=3, concurrency=2)},
        clock=clock,
        sleep=clock.sleep,
    )
    key = "u8.po.create_draft"

    assert limiter.daily_remaining(key) == 3
    assert limiter.reserve_budget(key, 2) is True
    assert limiter.reserve_budget(key, 2) is False  # 只剩 1，明确失败而不是超发
    assert limiter.daily_remaining(key) == 1


def test_concurrency_drops_when_remaining_below_20_percent() -> None:
    clock = FakeClock()
    limiter = ActionRateLimiter(
        {"u8.po.create_draft": RateLimit(qps=10.0, daily_quota=100, concurrency=10)},
        clock=clock,
        sleep=clock.sleep,
    )
    key = "u8.po.create_draft"

    async def scenario() -> None:
        for _ in range(9):
            await limiter.acquire("u8.po.create_draft")
        # 日配额仍充足，但令牌桶只剩 1/10 = 10% < 20%
        assert limiter.remaining_ratio(key) < 0.2
        assert limiter.suggested_concurrency(key) == 5

    assert limiter.suggested_concurrency(key) == 10
    run(scenario())


def test_daily_quota_exhausted_raises_rate_limited() -> None:
    clock = FakeClock()
    limiter = ActionRateLimiter(
        {"u8.po.create_draft": RateLimit(qps=100.0, daily_quota=1, concurrency=1)},
        clock=clock,
        sleep=clock.sleep,
    )

    async def scenario() -> None:
        await limiter.acquire("u8.po.create_draft")
        with pytest.raises(ConnectorError) as caught:
            await limiter.acquire("u8.po.create_draft")
        assert caught.value.kind is ErrorKind.RATE_LIMITED
        # 日配额耗尽不在请求路径里硬等
        assert caught.value.retry_after_s is not None
        assert caught.value.retry_after_s > 60

    run(scenario())


def test_undeclared_action_key_is_a_programming_error() -> None:
    limiter = ActionRateLimiter({})
    with pytest.raises(KeyError):
        limiter.remaining_ratio("u8.unknown.action")
