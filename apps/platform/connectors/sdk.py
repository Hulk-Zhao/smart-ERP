"""C2 连接器 SDK 契约（六能力 + ActionSpec 载体）。

对外可调用的写入口只有 ``BaseConnector.write()``，它强制绑定 ``ticket_id`` 与
``idem_key``，并把真正的传输层调用收敛到 ``_transport_write()``。
传输层实体在 ``apps.platform.connectors.transport``，只允许本模块与
``apps.platform.policy.gateway`` 导入（import-linter + AST 测试双重约束）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence

from apps.platform.connectors.transport import NullTransport, TransportRequest
from apps.platform.policy.levels import ActionSpec, RateLimit, VerifySpec

AccessLevel = Literal["L1", "L2", "L3"]
SecurityLevel = Literal["A", "B"]
Completeness = Literal["complete", "partial", "unavailable"]


class ConnectorContractError(Exception):
    """连接器实现未满足 SDK 契约。"""


class MissingTicketContext(ConnectorContractError):
    """无工单的写调用不存在（I-1）。"""


class MissingIdempotencyKey(ConnectorContractError):
    """无幂等键的写调用不存在（G-02）。"""


class UnknownOutcome(ConnectorContractError):
    """调用结果未知：不得重试，先走 verify（§3.1.4）。"""


@dataclass(frozen=True, slots=True)
class ConnectorMeta:
    system: str
    version: str
    access_level: AccessLevel
    security_level: SecurityLevel
    supports_obo: bool
    version_fingerprint: str
    experimental: bool = False


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    objects: tuple[str, ...]
    actions: tuple[ActionSpec, ...]
    authority_map: Mapping[str, str] = field(default_factory=dict)
    rate_limit: RateLimit = field(default_factory=RateLimit)


@dataclass(frozen=True, slots=True)
class ReadQuery:
    filters: Mapping[str, Any] = field(default_factory=dict)
    limit: int = 100


@dataclass(frozen=True, slots=True)
class DegradedReason:
    source: str
    reason: str
    from_at: str | None = None


@dataclass(frozen=True, slots=True)
class ReadResult:
    items: tuple[Mapping[str, Any], ...]
    completeness: Completeness
    degraded: tuple[DegradedReason, ...] = ()

    def __post_init__(self) -> None:
        # 读不到 ≠ 读到空：unavailable 必须带退化原因，禁止伪装成空集。
        if self.completeness == "unavailable" and not self.degraded:
            raise ConnectorContractError("unavailable 必须携带 degraded 原因（I-4）")


@dataclass(frozen=True, slots=True)
class FieldDiffEntry:
    field: str
    before: Any
    after: Any
    delta: Any | None = None


@dataclass(frozen=True, slots=True)
class PreviewResult:
    diff: tuple[FieldDiffEntry, ...]
    before: Mapping[str, Any] | None
    verify_spec: VerifySpec


@dataclass(frozen=True, slots=True)
class WriteResult:
    external_id: str | None
    response: Mapping[str, Any]
    before_ref: str | None = None
    after_ref: str | None = None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    status: Literal["match", "absent", "mismatch"]
    observed: Mapping[str, Any] | None = None

    def matches(self) -> bool:
        return self.status == "match"

    def absent(self) -> bool:
        return self.status == "absent"


@dataclass(frozen=True, slots=True)
class CompensateResult:
    status: Literal["compensated", "unchanged", "failed"]
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Health:
    ok: bool
    last_sync_at: str | None
    failure_rate: float
    rate_limit_remaining: float
    version_fingerprint: str


class Connector(Protocol):
    """§3.5.1 的六能力（describe 单列，其余为动作生命周期）。"""

    meta: ConnectorMeta

    def describe(self) -> CapabilitySet: ...

    async def read(self, entity: str, query: ReadQuery) -> ReadResult: ...

    async def preview(self, action: ActionSpec, payload: Mapping[str, Any]) -> PreviewResult: ...

    async def write(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult: ...

    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult: ...

    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult: ...

    async def health(self) -> Health: ...


class BaseConnector(ABC):
    """连接器实现基类：唯一写入口在这里，子类只实现 ``_transport_write`` 之外的能力。"""

    meta: ConnectorMeta

    def __init__(self, transport: Any | None = None) -> None:
        # 传输实例由 gateway/装配层注入；未注入时任何写调用都会失败（不是静默成功）。
        if transport is None:
            transport = NullTransport()
        self._transport = transport

    @abstractmethod
    def describe(self) -> CapabilitySet: ...

    @abstractmethod
    async def read(self, entity: str, query: ReadQuery) -> ReadResult: ...

    @abstractmethod
    async def preview(self, action: ActionSpec, payload: Mapping[str, Any]) -> PreviewResult: ...

    @abstractmethod
    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult: ...

    @abstractmethod
    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult: ...

    @abstractmethod
    async def health(self) -> Health: ...

    async def write(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        if not ticket_id:
            raise MissingTicketContext("写调用必须绑定 ticket_id（I-1）")
        if not idem_key:
            raise MissingIdempotencyKey("写调用必须携带 idem_key（G-02）")
        return await self._transport_write(action, payload, ticket_id=ticket_id, idem_key=idem_key)

    async def _transport_write(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        response = await self._transport.send(
            TransportRequest(
                action_key=action.key,
                payload=dict(payload),
                ticket_id=ticket_id,
                idem_key=idem_key,
            )
        )
        return WriteResult(
            external_id=response.external_id,
            response=response.body,
            before_ref=response.before_ref,
            after_ref=response.after_ref,
        )


def actions_of(capabilities: Sequence[CapabilitySet]) -> tuple[ActionSpec, ...]:
    """把多个连接器的能力集摊平成动作列表（注册表遍历用）。"""

    return tuple(action for caps in capabilities for action in caps.actions)
