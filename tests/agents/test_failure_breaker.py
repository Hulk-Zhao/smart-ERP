"""F1–F8 分类与熔断计数。"""

from __future__ import annotations

import pytest

from apps.agents.breaker import BreakerPolicy, CircuitBreaker
from apps.agents.failure import (
    FAILURES,
    TransientWriteError,
    classify_error_code,
    classify_exception,
    failure_of,
)
from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.gateway import (
    ApprovalRequiredError,
    BreakerState,
    PreviewFailedError,
    VerifyMismatchError,
)
from apps.platform.policy.levels import WriteLevel


def test_all_eight_failures_declared() -> None:
    assert set(FAILURES) == {f"F{index}" for index in range(1, 9)}
    for code, failure in FAILURES.items():
        assert failure.code == code
        assert failure.handling


def test_failure_lookup_rejects_unknown_code() -> None:
    with pytest.raises(ValueError):
        failure_of("F9")


def test_error_code_mapping_uses_frozen_contract() -> None:
    assert classify_error_code(ErrorCode.DATA_MISSING).code == "F1"  # type: ignore[union-attr]
    assert classify_error_code(ErrorCode.CONN_UNAVAILABLE).code == "F5"  # type: ignore[union-attr]
    assert classify_error_code(ErrorCode.EXTERNAL_EFFECTIVE).code == "F8"  # type: ignore[union-attr]
    assert classify_error_code(ErrorCode.POLICY_W3) is None
    assert classify_error_code(ErrorCode.AUTHZ_SCOPE) is None


def test_exception_classification() -> None:
    assert classify_exception(PreviewFailedError("a.b", "无前像")).code == "F1"  # type: ignore[union-attr]
    assert classify_exception(VerifyMismatchError("a.b")).code == "F6"  # type: ignore[union-attr]
    assert classify_exception(TransientWriteError("503")).code == "F5"  # type: ignore[union-attr]
    assert classify_exception(ApprovalRequiredError("t", WriteLevel.W1)) is None
    assert classify_exception(RuntimeError("unexpected")) is None


def test_breaker_opens_above_twenty_percent_and_caps_to_w0() -> None:
    now = [0.0]
    breaker = CircuitBreaker(BreakerPolicy(min_samples=5), clock=lambda: now[0])
    for _ in range(4):
        breaker.record("AG-1", ok=True)
    assert breaker.is_open("AG-1") is False

    breaker.record("AG-1", ok=False, fail_code="F5")
    # 1/5 = 20%，未「> 20%」
    assert breaker.state("AG-1") == BreakerState.HEALTHY

    breaker.record("AG-1", ok=False, fail_code="F5")
    assert breaker.state("AG-1") == BreakerState.W0_ONLY
    assert breaker.cap("AG-1") is WriteLevel.W0

    breaker.reset("AG-1")
    assert breaker.state("AG-1") == BreakerState.HEALTHY
    assert breaker.cap("AG-1") is WriteLevel.W2


def test_breaker_window_expires() -> None:
    now = [0.0]
    breaker = CircuitBreaker(BreakerPolicy(min_samples=2, window_seconds=60), clock=lambda: now[0])
    breaker.record("AG-1", ok=False, fail_code="F5")
    breaker.record("AG-1", ok=False, fail_code="F5")
    assert breaker.is_open("AG-1") is True
    now[0] = 3600.0
    assert breaker.is_open("AG-1") is False


def test_two_consecutive_f6_on_same_subject_opens_breaker() -> None:
    breaker = CircuitBreaker(BreakerPolicy(min_samples=100))
    breaker.record("AG-1", ok=False, fail_code="F6", subject="SUP-1")
    assert breaker.is_open("AG-1") is False
    breaker.record("AG-1", ok=False, fail_code="F6", subject="SUP-1")
    assert breaker.state("AG-1") == BreakerState.W0_ONLY
    # 其他场景不受影响
    assert breaker.is_open("AG-2") is False
    snapshot = breaker.snapshot("AG-1")
    assert snapshot["f6_streak"] == {"SUP-1": 2}
