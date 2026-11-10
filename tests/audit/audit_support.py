"""审计测试夹具：真实走 ``WriteGateway``，只把连接器替换为可观测的 Fake。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

from apps.audit.ledger import AuditLedger
from apps.audit.rollback import StepWrite
from apps.platform.connectors.sdk import (
    BaseConnector,
    CapabilitySet,
    ConnectorMeta,
    Health,
    PreviewResult,
    ReadQuery,
    ReadResult,
    VerifyResult,
    WriteResult,
)
from apps.platform.policy.gateway import WriteGateway
from apps.platform.policy.levels import (
    ActionSpec,
    CompensateSpec,
    RateLimit,
    SceneSpec,
    VerifySpec,
    WriteLevel,
)
from apps.platform.policy.registry import ActionRegistry


def run(coro):
    return asyncio.run(coro)


class AllowAllApprovals:
    """测试专用：审批链另由 tests/security 覆盖，这里只验证回滚通道本身。"""

    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        return True


def build_service(ledger: AuditLedger, connector: FakeConnector, approvals=None):
    from apps.audit.rollback import RollbackService

    return RollbackService(
        gateway=WriteGateway(connector, approvals=approvals or AllowAllApprovals()),
        ledger=ledger,
        scene=SceneSpec(code="AG-1", max_level=WriteLevel.W2),
    )


TENANT = UUID("11111111-1111-1111-1111-111111111111")
TICKET = UUID("22222222-2222-2222-2222-222222222222")


class FakeConnector(BaseConnector):
    meta = ConnectorMeta(
        system="fake",
        version="1.0",
        access_level="L1",
        security_level="A",
        supports_obo=True,
        version_fingerprint="fake-1",
    )

    def __init__(self) -> None:
        super().__init__()
        self.previews: list[tuple[str, Mapping[str, Any]]] = []
        self.writes: list[tuple[str, Mapping[str, Any], str, str]] = []

    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("invoice_draft",), actions=())

    async def read(self, entity: str, query: ReadQuery) -> ReadResult:
        return ReadResult(items=(), completeness="complete")

    async def preview(
        self, action: ActionSpec, payload: Mapping[str, Any]
    ) -> PreviewResult:
        self.previews.append((action.key, dict(payload)))
        return PreviewResult(
            diff=(),
            before={"draft_id": payload.get("draft_id"), "status": "draft"},
            verify_spec=action.verify
            or VerifySpec(kind="read_by_business_key", fields=("draft_id",)),
        )

    async def write(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        self.writes.append((action.key, dict(payload), ticket_id, idem_key))
        return WriteResult(
            external_id=f"ext-{len(self.writes)}",
            response={"draft_id": payload.get("draft_id"), "status": "compensated"},
            before_ref=f"s3://before/{len(self.writes)}",
            after_ref=f"s3://after/{len(self.writes)}",
        )

    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult:
        return VerifyResult(status="match", observed=dict(payload))

    async def compensate(self, comp: Any, *, idem_key: str):
        from apps.platform.connectors.sdk import CompensateResult

        return CompensateResult(status="compensated")

    async def health(self) -> Health:
        return Health(
            ok=True,
            last_sync_at=None,
            failure_rate=0.0,
            rate_limit_remaining=1.0,
            version_fingerprint="fake-1",
        )


def draft_action(
    *,
    key: str = "fake.invoice.create_draft",
    level: WriteLevel = WriteLevel.W1,
    compensation: str = "delete_draft",
) -> ActionSpec:
    return ActionSpec(
        key=key,
        level=level,
        writable_fields=frozenset({"draft_id", "note"}),
        compensation=CompensateSpec(kind=compensation),
        verify=VerifySpec(kind="read_by_business_key", fields=("draft_id",)),
        rate_limit=RateLimit(),
    )


async def seed_step(
    ledger: AuditLedger,
    *,
    action: ActionSpec,
    step_no: int,
    level: WriteLevel | None = None,
    object_id: str | None = None,
    step_id: UUID | None = None,
) -> StepWrite:
    sid = step_id or uuid4()
    object_key = object_id or f"D-{step_no}"
    image = await ledger.append_image(
        tenant_id=TENANT,
        ticket_id=TICKET,
        step_id=sid,
        system="fake",
        object_type="invoice_draft",
        object_id=object_key,
        before={"draft_id": object_key, "status": "new"},
        after={"draft_id": object_key, "status": "draft"},
        created_at=datetime(2026, 11, 10, 10, step_no, tzinfo=timezone.utc),
    )
    return StepWrite(
        step_id=sid,
        step_no=step_no,
        action_key=action.key,
        level=level or action.level,
        idem_key=f"idem-{step_no}",
        audit_image=image,
    )
