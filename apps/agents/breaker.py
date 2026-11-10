"""熔断计数与降级档位（FR-1107 / G-07）。

阈值口径（PRD AG-1 等场景表）：
- 最近 1 小时失败率 > 20% → 场景降为 W0_ONLY（只能预演/建议，写入 0 次）；
- 同一对象（供应商/单据）连续 2 次 F6 → 该场景 W0_ONLY；
- 失败率 > 50%（样本 ≥ min_samples）→ READONLY。

真实部署里 breaker_state 由 scheduler 落到 PG 并由 S8 观测；本模块是进程内计数，
接口故意做成 ``cap(scene_code) -> WriteLevel``，可直接喂给 ``WriteGateway`` 的
``breaker_cap``，保证「熔断期间写入 0 次」不依赖调用方记得检查。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from apps.platform.policy.gateway import BREAKER_CAP, BreakerState
from apps.platform.policy.levels import WriteLevel


@dataclass(frozen=True, slots=True)
class BreakerPolicy:
    window_seconds: float = 3600.0
    failure_rate: float = 0.20
    readonly_rate: float = 0.50
    min_samples: int = 5
    consecutive_f6: int = 2


@dataclass(slots=True)
class _Sample:
    at: float
    ok: bool
    fail_code: str | None
    subject: str | None


@dataclass(slots=True)
class _SceneSamples:
    samples: deque[_Sample] = field(default_factory=deque)
    f6_streak: dict[str, int] = field(default_factory=dict)


class CircuitBreaker:
    def __init__(
        self,
        policy: BreakerPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy or BreakerPolicy()
        self._clock = clock
        self._scenes: dict[str, _SceneSamples] = {}

    def _bucket(self, scene_code: str) -> _SceneSamples:
        return self._scenes.setdefault(scene_code, _SceneSamples())

    def _evict(self, bucket: _SceneSamples, now: float) -> None:
        cutoff = now - self._policy.window_seconds
        while bucket.samples and bucket.samples[0].at < cutoff:
            bucket.samples.popleft()

    def record(
        self,
        scene_code: str,
        *,
        ok: bool,
        fail_code: str | None = None,
        subject: str | None = None,
    ) -> None:
        bucket = self._bucket(scene_code)
        now = self._clock()
        bucket.samples.append(_Sample(now, ok, fail_code, subject))
        self._evict(bucket, now)

        if subject:
            if ok:
                bucket.f6_streak[subject] = 0
            elif fail_code == "F6":
                bucket.f6_streak[subject] = bucket.f6_streak.get(subject, 0) + 1
            else:
                bucket.f6_streak[subject] = 0

    def state(self, scene_code: str) -> str:
        bucket = self._scenes.get(scene_code)
        if bucket is None:
            return BreakerState.HEALTHY
        now = self._clock()
        self._evict(bucket, now)
        total = len(bucket.samples)
        if total >= self._policy.min_samples:
            failures = sum(1 for sample in bucket.samples if not sample.ok)
            rate = failures / total
            if rate > self._policy.readonly_rate:
                return BreakerState.READONLY
            if rate > self._policy.failure_rate:
                return BreakerState.W0_ONLY
        if any(streak >= self._policy.consecutive_f6 for streak in bucket.f6_streak.values()):
            return BreakerState.W0_ONLY
        return BreakerState.HEALTHY

    def is_open(self, scene_code: str) -> bool:
        return self.state(scene_code) != BreakerState.HEALTHY

    def cap(self, scene_code: str) -> WriteLevel:
        """直接作为 ``WriteGateway(breaker_cap=...)`` 的实现。"""

        return BREAKER_CAP[self.state(scene_code)]

    def snapshot(self, scene_code: str) -> dict[str, object]:
        bucket = self._scenes.get(scene_code)
        if bucket is None:
            return {"scene_code": scene_code, "state": BreakerState.HEALTHY, "samples": 0}
        now = self._clock()
        self._evict(bucket, now)
        total = len(bucket.samples)
        failures = sum(1 for sample in bucket.samples if not sample.ok)
        return {
            "scene_code": scene_code,
            "state": self.state(scene_code),
            "samples": total,
            "failures": failures,
            "failure_rate": (failures / total) if total else 0.0,
            "f6_streak": dict(bucket.f6_streak),
        }

    def reset(self, scene_code: str | None = None) -> None:
        if scene_code is None:
            self._scenes.clear()
        else:
            self._scenes.pop(scene_code, None)
