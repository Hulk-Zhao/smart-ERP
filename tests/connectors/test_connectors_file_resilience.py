"""文件通道的韧性：限流、重试退避、幂等、错误分类、超时与熔断。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import apps.connectors.file.transport as transport_module
from apps.connectors.breaker import BreakerPolicy
from apps.connectors.file.spec import READ_LIMIT_KEY
from apps.connectors.ratelimit import ActionRateLimiter
from apps.connectors.retry import RetryPolicy
from apps.platform.connectors.sdk import ReadQuery
from apps.platform.policy.levels import RateLimit
from tests.connectors.file_support import make_file_stack, run
from tests.connectors.support import FakeClock


def _read(stack, file: str, entity: str = "supplier_statement"):
    return run(stack.connector.read(entity, ReadQuery(filters={"file": file})))


def test_rate_limit_makes_the_second_read_wait() -> None:
    clock = FakeClock()
    limiter = ActionRateLimiter(
        {READ_LIMIT_KEY: RateLimit(qps=1.0, daily_quota=100, concurrency=1)},
        clock=clock,
        sleep=clock.sleep,
    )
    stack = make_file_stack(limiter=limiter)

    first = _read(stack, "statement_utf8.csv")
    second = _read(stack, "delivery_note_bom.csv", entity="delivery_note")

    assert first.completeness == "complete"
    assert second.completeness == "complete"
    assert clock.sleeps, "第二次读取必须排队等令牌，不能被限流放行"
    assert sum(clock.sleeps) >= 1.0
    assert limiter.daily_remaining(READ_LIMIT_KEY) == 98


def test_transient_error_is_retried_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    calls = {"count": 0}
    real = transport_module.read_bytes

    def flaky(path: Path) -> bytes:
        calls["count"] += 1
        if calls["count"] <= 2:
            raise OSError("共享目录暂时不可用")
        return real(path)

    monkeypatch.setattr(transport_module, "read_bytes", flaky)
    stack = make_file_stack(
        sleep=clock.sleep,
        retry_policy=RetryPolicy(
            max_attempts=3, base_delay_s=0.001, max_delay_s=0.01, jitter_ratio=0.0
        ),
    )

    result = _read(stack, "statement_utf8.csv")

    assert result.completeness == "complete"
    assert calls["count"] == 3
    assert clock.sleeps == [0.001, 0.002]


def test_permanent_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}
    real = transport_module.read_bytes

    def counting(path: Path) -> bytes:
        calls["count"] += 1
        return real(path)

    monkeypatch.setattr(transport_module, "read_bytes", counting)
    stack = make_file_stack(
        retry_policy=RetryPolicy(
            max_attempts=3, base_delay_s=0.001, max_delay_s=0.01, jitter_ratio=0.0
        )
    )

    result = _read(stack, "no_such_file.csv")

    assert result.completeness == "unavailable"
    assert result.degraded[0].reason == "read_failed:not_found"
    assert calls["count"] == 0, "not_found 是永久错误，不允许重试或读盘"
    assert stack.breaker.failures == 1


def test_idempotent_read_replays_same_content_only() -> None:
    stack = make_file_stack()

    first = _read(stack, "statement_utf8.csv")
    second = _read(stack, "statement_utf8.csv")

    assert first.items == second.items
    assert stack.transport.parse_count == 1, "同一份内容重复读必须命中幂等，不重复解析"


def test_same_path_overwritten_content_forces_reparse(tmp_path: Path) -> None:
    """负向用例：路径不变、内容原地覆盖时，必须重新解析，不能命中旧幂等键。"""

    path = tmp_path / "statement.csv"
    header = "doc_no,doc_date,supplier_code,item_code,qty,unit,unit_price,amount\n"
    path.write_text(header + "ST-1,2026-10-31,SUP-1,IT-1,100,PCS,12.5,1250\n", encoding="utf-8")

    stack = make_file_stack(root=tmp_path)
    first = _read(stack, "statement.csv")
    assert stack.transport.parse_count == 1
    assert first.items[0]["total_amount"]["amount"] == "1250.0000"

    # 同一个文件、同一个逻辑路径，只改内容。
    path.write_text(header + "ST-1,2026-10-31,SUP-1,IT-1,120,PCS,12.5,1500\n", encoding="utf-8")
    second = _read(stack, "statement.csv")

    assert stack.transport.parse_count == 2, "内容已变却命中旧幂等键 = 读回旧数据"
    assert second.items[0]["total_amount"]["amount"] == "1500.0000"
    assert second.items[0]["lines"][0]["quantity"] == "120.000000"


def test_breaker_opens_and_stops_file_access(tmp_path: Path) -> None:
    (tmp_path / "bad.csv").write_text("doc_no,doc_date\nST-1,2026-10-31\n", encoding="utf-8")
    stack = make_file_stack(
        root=tmp_path,
        breaker_policy=BreakerPolicy(failure_threshold=3, recovery_timeout_s=600.0),
    )

    for _ in range(3):
        result = _read(stack, "bad.csv")
        assert result.completeness == "unavailable"
    assert stack.breaker.state.value == "open"
    assert stack.transport.access_count == 3

    blocked = _read(stack, "bad.csv")

    assert blocked.completeness == "unavailable"
    assert blocked.degraded[0].reason == "read_failed:circuit_open"
    assert stack.transport.access_count == 3, "熔断打开后不允许再碰文件"


def test_timeout_is_classified_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    real = transport_module.read_bytes

    def slow(path: Path) -> bytes:
        time.sleep(0.2)
        return real(path)

    monkeypatch.setattr(transport_module, "read_bytes", slow)
    stack = make_file_stack(
        timeout_s=0.01,
        retry_policy=RetryPolicy(
            max_attempts=1, base_delay_s=0.001, max_delay_s=0.01, jitter_ratio=0.0
        ),
    )

    result = _read(stack, "statement_utf8.csv")

    assert result.completeness == "unavailable"
    assert result.degraded[0].reason == "read_failed:timeout"


def test_health_reports_sync_and_breaker_state(tmp_path: Path) -> None:
    stack = make_file_stack()
    _read(stack, "statement_utf8.csv")

    health = run(stack.connector.health())
    assert health.ok is True
    assert health.last_sync_at is not None
    assert health.failure_rate == 0.0
    assert 0.0 <= health.rate_limit_remaining <= 1.0
    assert health.version_fingerprint == stack.connector.meta.version_fingerprint

    (tmp_path / "bad.csv").write_text("doc_no,doc_date\nST-1,2026-10-31\n", encoding="utf-8")
    broken = make_file_stack(
        root=tmp_path,
        breaker_policy=BreakerPolicy(failure_threshold=1, recovery_timeout_s=600.0),
    )
    _read(broken, "bad.csv")

    assert run(broken.connector.health()).ok is False
