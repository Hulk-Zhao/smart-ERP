"""幂等键（G-02 / 技术架构 §3.6 第二层）。

语义（三层键里的「步骤/请求」层）：

- 同键同参（``request_hash`` 相同）→ 直接返回首次结果（replay），不重复发请求；
- 同键不同参 → ``E-IDEM-CONFLICT``（F7 之外的自有码），报障，不返回旧结果；
- 同键在途 → 拒绝并发重复执行。

生产落 ``erp.idempotency_record``（key 主键 + request_hash），本文件是进程内实现，
接口 ``IdempotencyStore`` 保持一致，换实现不改调用方。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from apps.connectors.errors import ConnectorError, ErrorKind


class IdemStatus(str, Enum):
    NEW = "new"
    REPLAY = "replay"
    IN_FLIGHT = "in_flight"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class IdemDecision:
    status: IdemStatus
    response: Mapping[str, Any] | None = None


def request_hash(payload: Mapping[str, Any]) -> str:
    """规范化 JSON 的 sha256：键排序、紧凑分隔符、Decimal 转字符串。

    同键不同参必须能被识别（G-02），所以这里不允许「差不多相等」。
    """

    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    request_hash: str
    status: str  # in_flight | succeeded | failed
    response: Mapping[str, Any] | None = None


class IdempotencyStore(Protocol):
    async def begin(self, key: str, fingerprint: str) -> IdemDecision: ...

    async def complete(self, key: str, response: Mapping[str, Any]) -> None: ...

    async def fail(self, key: str) -> None: ...


@dataclass
class InMemoryIdempotencyStore:
    """单进程实现；测试与 gateway 使用，生产替换为 PG 版本。"""

    _entries: dict[str, _Entry] = field(default_factory=dict)

    async def begin(self, key: str, fingerprint: str) -> IdemDecision:
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = _Entry(request_hash=fingerprint, status="in_flight")
            return IdemDecision(IdemStatus.NEW)
        if entry.request_hash != fingerprint:
            raise ConnectorError(
                ErrorKind.IDEM_CONFLICT,
                f"幂等键 {key} 已用于不同参数（G-02：同键不同参必须报障）",
            )
        if entry.status == "in_flight":
            raise ConnectorError(
                ErrorKind.IDEM_CONFLICT,
                f"幂等键 {key} 的首次请求仍在途，拒绝并发重复执行",
            )
        if entry.status == "succeeded" and entry.response is not None:
            return IdemDecision(IdemStatus.REPLAY, entry.response)
        # 上次失败：允许用同键同参重试。
        entry.status = "in_flight"
        entry.response = None
        return IdemDecision(IdemStatus.NEW)

    async def complete(self, key: str, response: Mapping[str, Any]) -> None:
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(request_hash=request_hash(response), status="succeeded")
            self._entries[key] = entry
        entry.status = "succeeded"
        entry.response = dict(response)

    async def fail(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is not None:
            entry.status = "failed"
            entry.response = None

    def peek(self, key: str) -> _Entry | None:
        return self._entries.get(key)
