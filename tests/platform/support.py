"""测试替身：不依赖真实连接器与数据库。"""

from __future__ import annotations

from typing import Any, Mapping

from apps.platform.connectors.sdk import (
    CapabilitySet,
    CompensateResult,
    ConnectorMeta,
    Health,
    PreviewResult,
    ReadQuery,
    ReadResult,
    VerifyResult,
    WriteResult,
)
from apps.platform.policy.levels import (
    ActionSpec,
    CompensateSpec,
    RateLimit,
    VerifySpec,
    WriteLevel,
)


class FakeConnector:
    meta = ConnectorMeta(
        system="fakerp",
        version="1.0.0",
        access_level="L1",
        security_level="A",
        supports_obo=False,
        version_fingerprint="sha256:fake",
    )

    def __init__(self, *, preview_before: Mapping[str, Any] | None = None) -> None:
        self.calls: list[str] = []
        self.preview_before = preview_before
        self.verify_status = "match"

    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("draft",), actions=())

    async def read(self, entity: str, query: ReadQuery) -> ReadResult:
        return ReadResult(items=(), completeness="complete")

    async def preview(self, action: ActionSpec, payload: Mapping[str, Any]) -> PreviewResult:
        self.calls.append("preview")
        return PreviewResult(
            diff=(),
            before=self.preview_before,
            verify_spec=VerifySpec(kind="read_by_external_id", fields=("draft_id",)),
        )

    async def write(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        self.calls.append("write")
        return WriteResult(
            external_id="D-001",
            response={"ticket_id": ticket_id, "idem_key": idem_key},
        )

    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult:
        self.calls.append("verify")
        return VerifyResult(status=self.verify_status)

    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult:
        return CompensateResult(status="compensated")

    async def health(self) -> Health:
        return Health(
            ok=True,
            last_sync_at=None,
            failure_rate=0.0,
            rate_limit_remaining=1.0,
            version_fingerprint=self.meta.version_fingerprint,
        )


class AllowAllApprovals:
    def __init__(self) -> None:
        self.seen: list[tuple[str, WriteLevel]] = []

    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        self.seen.append((ticket_id, level))
        return True


def make_action(
    level: WriteLevel = WriteLevel.W1,
    *,
    key: str = "fakerp.alias.save_draft",
    compensation: bool = True,
    verify: bool = True,
    writable: frozenset[str] = frozenset({"note", "draft_id"}),
    forbidden: frozenset[str] = frozenset(),
) -> ActionSpec:
    return ActionSpec(
        key=key,
        level=level,
        writable_fields=writable,
        forbidden_fields=forbidden,
        compensation=CompensateSpec(kind="delete_draft") if compensation else None,
        verify=VerifySpec(kind="read_by_external_id", fields=("draft_id",))
        if verify
        else None,
        rate_limit=RateLimit(qps=2.0, daily_quota=100, concurrency=1),
    )
