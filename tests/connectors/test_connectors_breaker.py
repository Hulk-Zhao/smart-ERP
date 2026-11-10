"""熔断：打开后零调用、等级降 W0、半开探针与恢复。"""

from __future__ import annotations

import pytest

from apps.connectors.breaker import BreakerPolicy, BreakerState, CircuitBreaker
from apps.connectors.errors import ConnectorError, ErrorKind
from apps.platform.policy.levels import WriteLevel
from tests.connectors.support import FakeClock


def test_opens_after_threshold_and_blocks_calls() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("u8", policy=BreakerPolicy(failure_threshold=3), clock=clock)

    for _ in range(3):
        assert breaker.allow() is True
        breaker.record_failure()

    assert breaker.state is BreakerState.OPEN
    assert breaker.allow() is False
    with pytest.raises(ConnectorError) as caught:
        breaker.require_closed()
    assert caught.value.kind is ErrorKind.CIRCUIT_OPEN


def test_open_breaker_caps_write_level_to_w0() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("u8", policy=BreakerPolicy(failure_threshold=1), clock=clock)

    assert breaker.write_cap() is WriteLevel.W2
    breaker.record_failure()
    # 熔断期间写入等级必须降到 W0（FR-1206）
    assert breaker.write_cap() is WriteLevel.W0


def test_half_open_allows_single_probe_then_recovers() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        "u8",
        policy=BreakerPolicy(failure_threshold=1, recovery_timeout_s=30.0),
        clock=clock,
    )
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN

    clock.now += 30.0
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow() is True
    breaker.require_closed()
    # 半开只放一个探针
    assert breaker.allow() is False

    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.write_cap() is WriteLevel.W2


def test_failed_probe_reopens() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        "u8",
        policy=BreakerPolicy(failure_threshold=2, recovery_timeout_s=10.0),
        clock=clock,
    )
    breaker.record_failure()
    breaker.record_failure()
    clock.now += 10.0
    assert breaker.state is BreakerState.HALF_OPEN

    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.write_cap() is WriteLevel.W0
