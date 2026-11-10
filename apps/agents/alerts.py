"""告警出口：P0/P1 与 recovered 指标统一走一个薄接口，便于测试捕获。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class Alert:
    level: Literal["P0", "P1", "info"]
    code: str
    message: str
    subject: str | None = None


class AlertSink(Protocol):
    def emit(self, alert: Alert) -> None: ...


class NullAlertSink:
    def emit(self, alert: Alert) -> None:
        return None


class RecordingAlertSink:
    """演练/测试用：把告警留在内存里，便于断言「报警而不是静默」。"""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def emit(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def codes(self) -> list[str]:
        return [alert.code for alert in self.alerts]
