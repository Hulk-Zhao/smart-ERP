"""观测层测试替身：不依赖 PG / 网络的最小连接器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from apps.platform.connectors.sdk import (
    CapabilitySet,
    CompensateResult,
    ConnectorMeta,
    Health,
    PreviewResult,
    ReadResult,
    VerifyResult,
    WriteResult,
)
from apps.platform.policy.levels import (
    ActionSpec,
    CompensateSpec,
    SceneSpec,
    VerifySpec,
    WriteLevel,
)


@dataclass
class FakeConnector:
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    compensations: list[str] = field(default_factory=list)
    meta: ConnectorMeta = field(
        default_factory=lambda: ConnectorMeta(
            system="u8",
            version="1.0",
            access_level="L2",
            security_level="A",
            supports_obo=True,
            version_fingerprint="fp-u8",
        )
    )

    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("purchase_order",), actions=())

    async def read(self, entity: str, query: Any) -> ReadResult:
        return ReadResult(items=(), completeness="complete")

    async def preview(
        self, action: ActionSpec, payload: Mapping[str, Any]
    ) -> PreviewResult:
        return PreviewResult(
            diff=(),
            before={"external_id": f"PRE-{action.key}", "snapshot": dict(payload)},
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
        self.writes.append((action.key, ticket_id, idem_key))
        return WriteResult(
            external_id=f"EXT-{len(self.writes)}",
            response={"ok": True, "http_status": 201},
            before_ref="BEFORE-1",
            after_ref="AFTER-1",
        )

    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult:
        return VerifyResult(status="match", observed={"ok": True})

    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult:
        self.compensations.append(idem_key)
        return CompensateResult(status="compensated")

    async def health(self) -> Health:
        return Health(
            ok=True,
            last_sync_at=None,
            failure_rate=0.0,
            rate_limit_remaining=100.0,
            version_fingerprint="fp-u8",
        )


class AllowAll:
    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        return True


def make_action(
    key: str = "u8.purchase_order.create_draft",
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
PAYLOAD = {"supplier": "S-001", "amount": "1200.00"}
