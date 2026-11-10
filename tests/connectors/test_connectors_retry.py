"""重试与退避：只重试临时错误，未知结果绝不重试。"""

from __future__ import annotations

import random

import pytest

from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.retry import RetryPolicy, backoff_delay, retry_async
from tests.connectors.support import run


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_transient_failure_retried_until_success() -> None:
    recorder = Recorder()
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectorError(ErrorKind.TRANSIENT, "连接抖动")
        return "ok"

    result = run(
        retry_async(
            flaky,
            policy=RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=8.0),
            sleep=recorder.sleep,
            rng=random.Random(7),
        )
    )

    assert result == "ok"
    assert attempts["n"] == 3
    assert len(recorder.sleeps) == 2
    # 指数退避：第二次等待严格长于第一次
    assert recorder.sleeps[1] > recorder.sleeps[0] >= 0.5


def test_transient_failure_gives_up_after_max_attempts() -> None:
    recorder = Recorder()
    attempts = {"n": 0}

    async def always_fail() -> None:
        attempts["n"] += 1
        raise ConnectorError(ErrorKind.TRANSIENT, "一直失败")

    with pytest.raises(ConnectorError):
        run(
            retry_async(
                always_fail,
                policy=RetryPolicy(max_attempts=3),
                sleep=recorder.sleep,
                rng=random.Random(1),
            )
        )
    assert attempts["n"] == 3


@pytest.mark.parametrize("kind", [ErrorKind.AUTH, ErrorKind.FORBIDDEN, ErrorKind.SCHEMA_CHANGED])
def test_permanent_errors_are_not_retried(kind: ErrorKind) -> None:
    recorder = Recorder()
    attempts = {"n": 0}

    async def fail() -> None:
        attempts["n"] += 1
        raise ConnectorError(kind, "永久失败")

    with pytest.raises(ConnectorError):
        run(retry_async(fail, sleep=recorder.sleep))
    assert attempts["n"] == 1
    assert recorder.sleeps == []


def test_unknown_outcome_is_never_retried() -> None:
    recorder = Recorder()
    attempts = {"n": 0}

    async def fail() -> None:
        attempts["n"] += 1
        raise ConnectorError(ErrorKind.UNKNOWN_OUTCOME, "写入结果未知")

    with pytest.raises(ConnectorError):
        run(retry_async(fail, sleep=recorder.sleep))
    # 未知结果必须走 verify，重试可能重复建单
    assert attempts["n"] == 1
    assert recorder.sleeps == []


def test_retry_after_header_wins_over_local_backoff() -> None:
    recorder = Recorder()
    attempts = {"n": 0}

    async def fail() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectorError(ErrorKind.RATE_LIMITED, "被限流", retry_after_s=3.0)
        return "ok"

    assert run(retry_async(fail, sleep=recorder.sleep)) == "ok"
    assert recorder.sleeps == [3.0]


def test_long_retry_after_is_not_waited_inline() -> None:
    recorder = Recorder()
    attempts = {"n": 0}

    async def fail() -> None:
        attempts["n"] += 1
        raise ConnectorError(ErrorKind.RATE_LIMITED, "日配额耗尽", retry_after_s=3600.0)

    with pytest.raises(ConnectorError):
        run(retry_async(fail, sleep=recorder.sleep))
    assert attempts["n"] == 1
    assert recorder.sleeps == []


def test_backoff_capped_and_jitter_bounded() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay_s=1.0, max_delay_s=8.0, jitter_ratio=0.25)
    rng = random.Random(11)
    for attempt in range(1, 8):
        delay = backoff_delay(attempt, policy, rng=rng)
        nominal = min(1.0 * (2 ** (attempt - 1)), 8.0)
        assert nominal <= delay <= nominal * 1.25
