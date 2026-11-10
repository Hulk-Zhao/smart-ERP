"""连接器错误分类（技术架构 §3.5.5 + C10 错误码）。

三类收敛口径（决定运行时行为，不是日志标签）：

| 类别 | 含哪些 kind | 重试 | 收敛动作 |
|---|---|---|---|
| ``transient`` | transient / rate_limited / timeout | 指数退避 + jitter，最多 3 次 | 仍失败 → F5，进补偿 |
| ``permanent`` | auth / forbidden / not_found / schema_changed / conflict / circuit_open / idem_conflict | 不重试 | 按 kind 各自处理（降 W0、拦截记录、F7…） |
| ``unknown`` | unknown_outcome | **不重试** | 先 ``verify`` 再决定（§3.1.4），避免重复写入 |

``fail_code`` 对齐 F1–F8（PRD §6.3）：读不到数据是 F1，连接器不可用是 F5，
版本漂移是 F5 但不可重试，冲突是 F7，外部已生效是 F8。
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

try:  # httpx 是连接器运行时的既定依赖（api 镜像与 gw 镜像都装）
    import httpx
except ImportError:  # pragma: no cover - 仅在裁剪镜像里可能缺失
    httpx = None  # type: ignore[assignment]


class ErrorKind(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    AUTH = "auth"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    INVALID = "invalid_request"
    SCHEMA_CHANGED = "schema_changed"
    CONFLICT = "conflict"
    CIRCUIT_OPEN = "circuit_open"
    IDEM_CONFLICT = "idem_conflict"
    UNKNOWN_OUTCOME = "unknown_outcome"


class ErrorClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


_TRANSIENT_KINDS: frozenset[ErrorKind] = frozenset(
    {ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED, ErrorKind.TIMEOUT}
)
_PERMANENT_KINDS: frozenset[ErrorKind] = frozenset(
    {
        ErrorKind.AUTH,
        ErrorKind.FORBIDDEN,
        ErrorKind.NOT_FOUND,
        ErrorKind.INVALID,
        ErrorKind.SCHEMA_CHANGED,
        ErrorKind.CONFLICT,
        ErrorKind.CIRCUIT_OPEN,
        ErrorKind.IDEM_CONFLICT,
    }
)

# F 码：连接器侧只产出这几种；F6（写入值不符）由 WriteGateway 的 verify 判定后给出。
_FAIL_CODES: dict[ErrorKind, str | None] = {
    ErrorKind.TRANSIENT: "F5",
    ErrorKind.RATE_LIMITED: "F5",
    ErrorKind.TIMEOUT: "F5",
    ErrorKind.AUTH: "F5",
    ErrorKind.FORBIDDEN: "F5",
    ErrorKind.INVALID: "F5",
    ErrorKind.SCHEMA_CHANGED: "F5",
    ErrorKind.NOT_FOUND: "F1",
    ErrorKind.CONFLICT: "F7",
    ErrorKind.IDEM_CONFLICT: None,
    ErrorKind.CIRCUIT_OPEN: "F5",
    ErrorKind.UNKNOWN_OUTCOME: "F8",
}


def error_class_of(kind: ErrorKind) -> ErrorClass:
    if kind in _TRANSIENT_KINDS:
        return ErrorClass.TRANSIENT
    if kind in _PERMANENT_KINDS:
        return ErrorClass.PERMANENT
    return ErrorClass.UNKNOWN


class ConnectorError(Exception):
    """连接器调用失败。``kind`` 是唯一判定口径，调用方不得自行按 message 猜。"""

    def __init__(
        self,
        kind: ErrorKind | str,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        connector: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = ErrorKind(kind) if not isinstance(kind, ErrorKind) else kind
        self.message = message
        self.status = status
        self.retry_after_s = retry_after_s
        self.connector = connector
        self.detail = detail or {}

    @property
    def error_class(self) -> ErrorClass:
        return error_class_of(self.kind)

    @property
    def retryable(self) -> bool:
        return self.error_class is ErrorClass.TRANSIENT

    @property
    def fail_code(self) -> str | None:
        return _FAIL_CODES[self.kind]

    @property
    def unknown_outcome(self) -> bool:
        return self.error_class is ErrorClass.UNKNOWN

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"[{self.kind.value}] {self.message}"


def classify_status_code(status: int) -> ErrorKind:
    """HTTP 状态 → 错误类别。调用方不许再写第二套映射。"""

    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (401,):
        return ErrorKind.AUTH
    if status == 403:
        return ErrorKind.FORBIDDEN
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status == 409:
        return ErrorKind.CONFLICT
    if status in (400, 405, 406, 410, 422):
        return ErrorKind.INVALID
    if status >= 500:
        return ErrorKind.TRANSIENT
    return ErrorKind.INVALID


def _is_httpx(exc: BaseException, *names: str) -> bool:
    if httpx is None:
        return False
    return any(isinstance(exc, getattr(httpx, name)) for name in names if hasattr(httpx, name))


def classify_exception(exc: BaseException, *, phase: str = "read") -> ConnectorError:
    """异常 → ``ConnectorError``。

    ``phase="write"`` 时「请求可能已到达目标系统」的异常归为 ``unknown_outcome``：
    这类失败**不允许重试**，必须走 verify（§3.1.4），否则会产生重复单据。
    """

    write = phase == "write"

    if isinstance(exc, ConnectorError):
        return exc

    if _is_httpx(exc, "ConnectTimeout", "ConnectError", "PoolTimeout"):
        # 连接未建立：请求一定没有到达目标，可安全重试。
        return ConnectorError(ErrorKind.TRANSIENT, f"连接失败: {exc}")
    if _is_httpx(exc, "ReadTimeout", "WriteTimeout", "RemoteProtocolError", "ReadError"):
        if write:
            return ConnectorError(ErrorKind.UNKNOWN_OUTCOME, f"写入结果未知: {exc}")
        return ConnectorError(ErrorKind.TIMEOUT, f"读取超时: {exc}")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        if write:
            return ConnectorError(ErrorKind.UNKNOWN_OUTCOME, "写入超时，结果未知")
        return ConnectorError(ErrorKind.TIMEOUT, "读取超时")
    if _is_httpx(exc, "InvalidURL", "UnsupportedProtocol"):
        return ConnectorError(ErrorKind.INVALID, f"请求非法: {exc}")
    if _is_httpx(exc, "DecodingError"):
        # 响应结构与声明不符：按版本漂移处理（FR-1206 会降到 W0）。
        return ConnectorError(ErrorKind.SCHEMA_CHANGED, f"响应无法解析: {exc}")
    if isinstance(exc, ValueError) and "json" in str(exc).lower():
        return ConnectorError(ErrorKind.SCHEMA_CHANGED, f"响应不是合法 JSON: {exc}")

    if write:
        return ConnectorError(ErrorKind.UNKNOWN_OUTCOME, f"写入结果未知: {exc!r}")
    return ConnectorError(ErrorKind.TRANSIENT, f"未知读取失败: {exc!r}")
