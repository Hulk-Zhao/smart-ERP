"""产品 API 测试夹具：真实走 AccessGate / WriteGateway，只替换连接器与内存仓储。

关键约束：连接器是唯一可观测的外部副作用点。所有拒绝类用例都断言
``connector.writes == []``（以及 ``connector.previews == []``），证明拒绝发生在
四道闸/写闸门之前，而不是「写完再回滚」。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from apps.api import configure_container, reset_container
from apps.api.container import ApiContainer
from apps.api.recon_store import FileReconStore
from apps.api.store import PendingWrite, TicketRecord
from apps.main import create_app
from apps.platform.config import Settings
from apps.platform.connectors.sdk import (
    BaseConnector,
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
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.levels import (
    ActionSpec,
    CompensateSpec,
    RateLimit,
    SceneSpec,
    VerifySpec,
    WriteLevel,
)
from apps.security.identity import ScopeGrant, UserPrincipal

TENANT = UUID("11111111-1111-1111-1111-111111111111")
ORG = "ORG-1"
PERIOD = "2026-10"
SYSTEM = "u8"
STATEMENT = "statement"
SCENE = "ag2_processing_fee"
PREFIX = "/erp/api/v1"

INITIATOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
APPROVER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
AUDITOR = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


class FakeConnector(BaseConnector):
    """可观测替身：记录预演/写入/校验次数，写入本身不产生真实副作用。"""

    meta = ConnectorMeta(
        system=SYSTEM,
        version="1.0.0",
        access_level="L2",
        security_level="A",
        supports_obo=True,
        version_fingerprint="sha256:fake-u8",
    )

    def __init__(self) -> None:
        super().__init__()
        self.previews: list[tuple[str, Mapping[str, Any]]] = []
        self.writes: list[tuple[str, Mapping[str, Any], str, str]] = []
        self.verifies: list[str] = []

    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("draft",), actions=())

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
            or VerifySpec(kind="read_by_external_id", fields=("draft_id",)),
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
            response={"draft_id": payload.get("draft_id"), "status": "draft"},
            before_ref=f"mem://before/{len(self.writes)}",
            after_ref=f"mem://after/{len(self.writes)}",
        )

    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult:
        self.verifies.append(spec.kind)
        return VerifyResult(status="match", observed=dict(payload))

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


def _forward_action() -> ActionSpec:
    return ActionSpec(
        key="u8.trade.save_draft",
        level=WriteLevel.W1,
        writable_fields=frozenset({"draft_id", "note"}),
        compensation=CompensateSpec(
            kind="delete_draft", action_key="u8.trade.delete_draft"
        ),
        verify=VerifySpec(kind="read_by_external_id", fields=("draft_id",)),
        rate_limit=RateLimit(),
    )


def _reverse_action() -> ActionSpec:
    # 反向动作 W0（不得高于原动作等级）；W0 不强制声明补偿。
    return ActionSpec(
        key="u8.trade.delete_draft",
        level=WriteLevel.W0,
        writable_fields=frozenset({"draft_id"}),
        verify=VerifySpec(kind="read_by_external_id", fields=("draft_id",)),
        rate_limit=RateLimit(),
    )


def _principal(user_id: UUID, roles: frozenset[str]) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=TENANT,
        user_id=user_id,
        roles=roles,
        grant=ScopeGrant(
            org_ids=frozenset({ORG}),
            periods=frozenset({PERIOD}),
            system_ids=frozenset({SYSTEM, STATEMENT}),
        ),
    )


@dataclass
class ApiEnv:
    client: TestClient
    container: ApiContainer
    connector: FakeConnector
    tickets: dict[str, UUID]
    tokens: dict[str, str] = field(default_factory=dict)
    recon_store: FileReconStore | None = None
    recon_store_path: Path | None = None

    def headers(self, who: str = "initiator", idem: str | None = None) -> dict[str, str]:
        headers = {"X-Session-Token": self.tokens[who]}
        if idem is not None:
            headers["Idempotency-Key"] = idem
        return headers

    def scope(self, **overrides: Any) -> dict[str, Any]:
        scope: dict[str, Any] = {
            "org_id": ORG,
            "period": PERIOD,
            "system_ids": [SYSTEM],
        }
        scope.update(overrides)
        return scope

    def scope_params(self, **overrides: Any) -> list[tuple[str, str]]:
        scope = self.scope(**overrides)
        params: list[tuple[str, str]] = [
            ("org_id", scope["org_id"]),
            ("period", scope["period"]),
        ]
        params.extend(("system_ids", system) for system in scope["system_ids"])
        return params


def _ticket(
    *,
    ticket_no: str,
    level: WriteLevel,
    state: TicketState,
    amount: str,
    pending: PendingWrite | None = None,
    org_id: str = ORG,
) -> TicketRecord:
    return TicketRecord(
        ticket_no=ticket_no,
        tenant_id=TENANT,
        scene_code=SCENE,
        initiator_id=INITIATOR,
        business_key=f"BK-{ticket_no}",
        org_id=org_id,
        period=PERIOD,
        systems=(SYSTEM,),
        write_level=level,
        amount=Decimal(amount),
        touches_fund=False,
        state=state,
        created_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
        pending_write=pending,
    )


@pytest.fixture
def env(tmp_path: Path) -> ApiEnv:
    connector = FakeConnector()
    store_path = tmp_path / "recon-store.json"
    store = FileReconStore(store_path)
    container = ApiContainer(
        connectors={SYSTEM: connector},
        scenes={SCENE: SceneSpec(code=SCENE, max_level=WriteLevel.W2)},
        recon_store=store,
    )
    container.actions.register(_reverse_action())
    container.actions.register(_forward_action())

    tickets = {
        # 正常 W1：审批通过后执行一次外部写入。
        "normal": _ticket(
            ticket_no="T-1001",
            level=WriteLevel.W1,
            state=TicketState.PENDING_APPROVAL,
            amount="1200.00",
            pending=PendingWrite(
                action_key="u8.trade.save_draft",
                system=SYSTEM,
                payload={"draft_id": "D-1001", "note": "首单"},
                idem_key="idem-t1001",
            ),
        ),
        # W3：永远拒绝（无执行路径）。
        "w3": _ticket(
            ticket_no="T-1002",
            level=WriteLevel.W3,
            state=TicketState.PENDING_APPROVAL,
            amount="100.00",
            pending=PendingWrite(
                action_key="u8.trade.save_draft",
                system=SYSTEM,
                payload={"draft_id": "D-1002", "note": "资金动作"},
                idem_key="idem-t1002",
            ),
        ),
        # W2 + 大额：需要 reviewer + approver 两个不同审批人。
        "dual": _ticket(
            ticket_no="T-1003",
            level=WriteLevel.W2,
            state=TicketState.PENDING_APPROVAL,
            amount="200000.00",
            pending=PendingWrite(
                action_key="u8.trade.save_draft",
                system=SYSTEM,
                payload={"draft_id": "D-1003", "note": "大额"},
                idem_key="idem-t1003",
            ),
        ),
        # 驳回用。
        "reject": _ticket(
            ticket_no="T-1004",
            level=WriteLevel.W1,
            state=TicketState.PENDING_APPROVAL,
            amount="300.00",
        ),
        # 终止用（尚无外部写入）。
        "cancel": _ticket(
            ticket_no="T-1005",
            level=WriteLevel.W1,
            state=TicketState.PLANNED,
            amount="50.00",
        ),
        # 其他组织：用于证明读接口按 scope 过滤、不会泄露。
        "other_org": _ticket(
            ticket_no="T-1006",
            level=WriteLevel.W1,
            state=TicketState.PENDING_APPROVAL,
            amount="10.00",
            org_id="ORG-2",
        ),
    }
    for ticket in tickets.values():
        container.tickets.add(ticket)

    tokens = {
        "initiator": container.session_directory.issue(
            container.broker, _principal(INITIATOR, frozenset({"operator"}))
        ),
        "reviewer": container.session_directory.issue(
            container.broker, _principal(REVIEWER, frozenset({"reviewer", "finance"}))
        ),
        "approver": container.session_directory.issue(
            container.broker, _principal(APPROVER, frozenset({"approver", "finance"}))
        ),
        "auditor": container.session_directory.issue(
            container.broker, _principal(AUDITOR, frozenset({"auditor"}))
        ),
    }

    configure_container(container)
    app = create_app(Settings(image_tag="test"))
    client = TestClient(app)
    try:
        yield ApiEnv(
            client=client,
            container=container,
            connector=connector,
            tickets={name: ticket.ticket_id for name, ticket in tickets.items()},
            tokens=tokens,
            recon_store=store,
            recon_store_path=store_path,
        )
    finally:
        reset_container()


__all__ = [
    "APPROVER",
    "AUDITOR",
    "INITIATOR",
    "ORG",
    "PERIOD",
    "PREFIX",
    "REVIEWER",
    "SCENE",
    "STATEMENT",
    "SYSTEM",
    "TENANT",
    "ApiEnv",
    "env",
]
