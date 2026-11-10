"""连接器传输层（私有）。

**不要从外部模块导入本模块。** 全系统只有两处允许依赖它：

- ``apps.platform.connectors.sdk`` —— ``BaseConnector._transport_write()``；
- ``apps.platform.policy.gateway`` —— 写入闸门装配。

约束由 pyproject 的 import-linter 契约 + ``tests/platform/test_connector_transport_isolation.py``
的全量 AST 扫描共同保证（§3.2.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


class TransportError(Exception):
    """传输失败。``kind`` 决定运行时是否重试（§3.5.5）。"""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        fail_code: str = "F5",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.fail_code = fail_code
        self.status = status


class UnconfiguredTransport(TransportError):
    """未装配传输实例时，任何写调用都必须失败。"""

    def __init__(self) -> None:
        super().__init__("auth", "连接器未装配 transport，拒绝写入", retryable=False)


@dataclass(frozen=True, slots=True)
class TransportRequest:
    action_key: str
    payload: Mapping[str, Any]
    ticket_id: str
    idem_key: str


@dataclass(frozen=True, slots=True)
class TransportResponse:
    external_id: str | None
    body: Mapping[str, Any] = field(default_factory=dict)
    before_ref: str | None = None
    after_ref: str | None = None


class ConnectorTransport(Protocol):
    async def send(self, request: TransportRequest) -> TransportResponse: ...


class NullTransport:
    """显式未配置的传输：先失败再从长计议，绝不假装写入成功。"""

    async def send(self, request: TransportRequest) -> TransportResponse:
        raise UnconfiguredTransport()
