"""熔断（G-07 / FR-1107 / FR-1206）。

两种收敛都靠它，所以状态机只允许一个实现：

- 调用侧：打开后**不再发出任何请求**（熔断期间写入 0 次，FR-1107 验收）；
- 策略侧：``write_cap()`` 返回 W0，喂给 ``effective_level`` 的第四路，
  与 S0 的 ``BREAKER_CAP`` 语义一致（配置只能降，不能借熔断抬高等级）。

状态真源在生产环境落 PG ``breaker_state``（由 scheduler 聚合），本类是进程内实现。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum

from apps.connectors.errors import ConnectorError, ErrorKind
from apps.platform.policy.levels import WriteLevel


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerPolicy:
    """连续失败 ``failure_threshold`` 次打开；``recovery_timeout_s`` 后放一个探针。"""

    __slots__ = ("failure_threshold", "half_open_max_calls", "recovery_timeout_s")

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 60.0,
        half_open_max_calls: int = 1,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold 至少为 1")
        if recovery_timeout_s < 0:
            raise ValueError("recovery_timeout_s 不能为负")
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.half_open_max_calls = half_open_max_calls


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        policy: BreakerPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.policy = policy or BreakerPolicy()
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_inflight = 0

    @property
    def state(self) -> BreakerState:
        if (
            self._state is BreakerState.OPEN
            and self._opened_at is not None
            and self._clock() - self._opened_at >= self.policy.recovery_timeout_s
        ):
            self._state = BreakerState.HALF_OPEN
            self._half_open_inflight = 0
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    def allow(self) -> bool:
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        return self._half_open_inflight < self.policy.half_open_max_calls

    def require_closed(self) -> None:
        """打开期间任何调用（含读）直接失败，不发请求。"""

        if not self.allow():
            raise ConnectorError(
                ErrorKind.CIRCUIT_OPEN,
                f"连接器 {self.name} 已熔断，本次零调用",
                detail={"state": self.state.value, "failures": self._failures},
            )
        if self.state is BreakerState.HALF_OPEN:
            self._half_open_inflight += 1

    def record_success(self) -> None:
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = None
        self._half_open_inflight = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self.state is BreakerState.HALF_OPEN:
            self._open()
            return
        if self._failures >= self.policy.failure_threshold:
            self._open()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._half_open_inflight = 0

    def write_cap(self) -> WriteLevel:
        """喂给 ``effective_level(breaker_cap=...)``：未恢复前最高 W0。"""

        return WriteLevel.W2 if self.state is BreakerState.CLOSED else WriteLevel.W0

    def reset(self) -> None:
        """管理员恢复（FR-1206「恢复等级需管理员确认」）——只允许显式调用。"""

        self.record_success()


class BreakerRegistry:
    """按连接器名持有熔断器；gateway 暴露的 ``breaker_cap`` 从这里取。"""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str, *, policy: BreakerPolicy | None = None) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, policy=policy)
        return self._breakers[name]

    def cap(self, name: str) -> WriteLevel:
        breaker = self._breakers.get(name)
        return breaker.write_cap() if breaker is not None else WriteLevel.W2
