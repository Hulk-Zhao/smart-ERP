"""健康度（FR-1205 / §3.5.1 ``health()``）。

只采集 SDK 契约里的五个字段：最后同步时间、失败率、限流余量、版本指纹、是否可用。
失败率是滑动窗口内的比例，不做「历史累计」——累计值永远恢复不了，会让连接器永久看起来有病。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from apps.platform.connectors.sdk import Health


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


@dataclass
class HealthTracker:
    system: str
    version_fingerprint: str
    window: int = 50
    _results: deque[bool] = field(default_factory=deque)
    _last_sync_at: str | None = None

    def note_success(self, *, synced: bool = False) -> None:
        self._record(True)
        if synced:
            self._last_sync_at = _now()

    def note_failure(self) -> None:
        self._record(False)

    def _record(self, ok: bool) -> None:
        self._results.append(ok)
        while len(self._results) > self.window:
            self._results.popleft()

    @property
    def failure_rate(self) -> float:
        if not self._results:
            return 0.0
        failed = sum(1 for ok in self._results if not ok)
        return failed / len(self._results)

    @property
    def last_sync_at(self) -> str | None:
        return self._last_sync_at

    def snapshot(self, *, ok: bool, rate_limit_remaining: float) -> Health:
        return Health(
            ok=ok,
            last_sync_at=self._last_sync_at,
            failure_rate=round(self.failure_rate, 4),
            rate_limit_remaining=round(max(0.0, min(1.0, rate_limit_remaining)), 4),
            version_fingerprint=self.version_fingerprint,
        )
