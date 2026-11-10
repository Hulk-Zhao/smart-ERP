"""文件通道的韧性：限流、重试退避、幂等、错误分类、超时与熔断。"""

from __future__ import annotations

import tempfile
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
from tests.connectors.file_support import FILE_FIXTURES, make_file_stack, run
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


def test_idempotent_read_parses_once_and_content_change_invalidates() -> None:
    stack = make_file_stack()

    first = _read(stack, "statement_utf8.csv")
    second = _read(stack, "statement_utf8.csv")

    assert first.items == second.items
    assert stack.transport.parse_count == 1, "同一份内容重复读必须命中幂等，不重复解析"

    # 内容变 → 哈希变 → 键变，返回新的解析结果。
    source = (FILE_FIXTURES / "statement_utf8.csv").read_text(encoding="utf-8")
    modified = source.replace("100,PCS,12.5,1250", "120,PCS,12.5,1500")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "statement_utf8.csv"
        copy.write_text(modified, encoding="utf-8")
        changed_stack = make_file_stack(root=Path(tmp))
        changed = _read(changed_stack, "statement_utf8.csv")
        assert changed_stack.transport.parse_count == 1

    assert changed.items[0]["total_amount"]["amount"] == "1900.0000"


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
