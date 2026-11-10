"""S3 运行时测试替身：脚本化连接器 + 常用夹具。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from apps.agents.failure import TransientWriteError
from apps.agents.runtime import TicketRuntime
from apps.platform.connectors.sdk import (
    CapabilitySet,
    CompensateResult,
    ConnectorMeta,
    Health,
    PreviewResult,
    ReadResult,
    UnknownOutcome,
    VerifyResult,
    WriteResult,
)
from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.levels import (
    ActionSpec,
    CompensateSpec,
    SceneSpec,
    VerifySpec,
    WriteLevel,
)


class ExternalEffectiveError(Exception):
    """目标系统回报「已生效」：不可回滚，只能锁定（F8）。"""

    code = ErrorCode.EXTERNAL_EFFECTIVE


@dataclass
class ScriptedConnector:
    """按动作键切换行为：

    - ``ok``：正常写入；
    - ``unknown_success``：抛 UnknownOutcome，但 verify 返回 match（超时但已成功）；
    - ``unknown_absent``：抛 UnknownOutcome，verify 返回 absent（未写入，可重放）；
    - ``unknown_mismatch``：抛 UnknownOutcome，verify 返回 mismatch（值不符）；
    - ``transient``：始终抛可重试失败；
    - ``f8``：抛「外部已生效」。
    """

    default_behaviour: str = "ok"
    behaviours: dict[str, str] = field(default_factory=dict)
    compensate_status: str = "compensated"
    compensate_failures: int = 0
    write_delay: float = 0.0

    meta: ConnectorMeta = field(
        default_factory=lambda: ConnectorMeta(
            system="fake",
            version="1.0",
            access_level="L1",
            security_level="A",
            supports_obo=False,
            version_fingerprint="fp-1",
        )
    )

    calls: list[tuple[str, str]] = field(default_factory=list)
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    compensations: list[str] = field(default_factory=list)
    verify_calls: int = 0
    _pending_verify: str | None = None

    # ---------------------------------------------------------------- 能力
    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("fake",), actions=())

    async def read(self, entity: str, query: Any) -> ReadResult:
        return ReadResult(items=(), completeness="complete")

    async def preview(
        self, action: ActionSpec, payload: Mapping[str, Any]
    ) -> PreviewResult:
        self.calls.append(("preview", action.key))
        return PreviewResult(
            diff=(),
            before={"external_id": f"PRE-{action.key}"},
            verify_spec=action.verify or VerifySpec("read_by_external_id"),
        )

    async def write(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        self.calls.append(("write", action.key))
        self.writes.append((action.key, ticket_id, idem_key))
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        behaviour = self.behaviours.get(action.key, self.default_behaviour)
        if behaviour == "ok":
            return WriteResult(
                external_id=f"EXT-{len(self.writes)}",
                response={"ok": True},
                before_ref="DRAFT-BEFORE",
                after_ref="DRAFT-AFTER",
            )
        if behaviour in {"unknown_success", "unknown_absent", "unknown_mismatch"}:
            self._pending_verify = behaviour
            raise UnknownOutcome(f"{action.key} 调用结果未知（模拟超时）")
        if behaviour == "transient":
            raise TransientWriteError("模拟 503（可重试）")
        if behaviour == "f8":
            raise ExternalEffectiveError("目标系统已生效")
        raise AssertionError(f"未知行为: {behaviour}")

    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult:
        self.verify_calls += 1
        self.calls.append(("verify", spec.kind))
        pending = self._pending_verify
        self._pending_verify = None
        if pending == "unknown_mismatch":
            return VerifyResult(status="mismatch", observed={"amount": "999"})
        if pending == "unknown_absent":
            return VerifyResult(status="absent")
        return VerifyResult(status="match", observed={"ok": True})

    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult:
        self.compensations.append(idem_key)
        self.calls.append(("compensate", getattr(comp, "action_key", "?")))
        if self.compensate_failures > 0:
            self.compensate_failures -= 1
            return CompensateResult(status="failed")
        return CompensateResult(status=self.compensate_status)  # type: ignore[arg-type]

    async def health(self) -> Health:
        return Health(ok=True, last_sync_at=None, failure_rate=0.0, rate_limit_remaining=100.0, version_fingerprint="fp-1")


class AlwaysApproved:
    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        return True


def make_action(
    key: str,
    *,
    level: WriteLevel = WriteLevel.W1,
    fields: tuple[str, ...] = ("supplier", "amount"),
) -> ActionSpec:
    return ActionSpec(
        key=key,
        level=level,
        writable_fields=frozenset(fields),
        compensation=CompensateSpec(kind="delete_draft"),
        verify=VerifySpec(kind="read_by_external_id", fields=("external_id",)),
    )


SCENE = SceneSpec("AG-1", WriteLevel.W1)


def fake_idem(action_key: str, seq: int = 1) -> str:
    digest = hashlib.sha256(f"{action_key}:{seq}".encode()).hexdigest()[:12]
    return f"idem-{digest}"


def build_runtime(
    env: Any,
    connector: ScriptedConnector,
    *,
    max_attempts: int = 3,
    breaker: Any = None,
    compensator: Any = None,
    compensation_stale_seconds: float = 60.0,
    compensation_retry_seconds: float = 0.0,
    lease_ttl_seconds: int = 30,
    alert_sink: Any = None,
) -> TicketRuntime:
    """把测试替身装配成运行时（写入仍走真实 WriteGateway）。"""

    from apps.platform.policy.gateway import WriteGateway

    gateway = WriteGateway(
        connector,
        approvals=AlwaysApproved(),
        breaker_cap=(breaker.cap if breaker is not None else None),
    )

    async def verify(planned: Any) -> Any:
        spec = planned.action.verify or VerifySpec("read_by_external_id")
        return await connector.verify(spec, planned.verify_payload or planned.payload)

    async def compensate(call: Any, idem_key: str) -> Any:
        return await connector.compensate(call, idem_key=idem_key)

    return TicketRuntime(
        store=env.store,
        leases=env.leases,
        gateway=gateway,
        compensation=env.queue,
        verifier=verify,
        compensator=compensator or compensate,
        max_attempts=max_attempts,
        retry_backoff_seconds=(0.0, 0.0, 0.0),
        compensation_retry_seconds=compensation_retry_seconds,
        compensation_stale_seconds=compensation_stale_seconds,
        lease_ttl_seconds=lease_ttl_seconds,
        alert_sink=alert_sink,
    )
