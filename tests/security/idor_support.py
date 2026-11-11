"""跨租户 / IDOR 扫描夹具：两个租户、同一组织与期间，只替换连接器与内存仓储。

设计要点：
- **租户 A** 是攻击者身份（operator / reviewer / auditor），**租户 B** 是被攻击资源；
  两个租户的 ``org_id / period / system_ids`` 完全一致，确保「scope 相同、租户不同」，
  这样任何泄露都只能归因于缺少租户校验，而不是 scope 校验。
- 资源覆盖工单、对账批次、差异、影像、审计链、导出、计量表七类；
- 连接器继续用 ``FakeConnector``，拒绝类断言可检查 ``previews/writes`` 为空。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from matrix_support import (
    ORG,
    PERIOD,
    PREFIX,
    SCENE,
    SYSTEM,
    FakeConnector,
    action_forward,
    action_reverse,
    meta_a,
)

from apps.api import configure_container, reset_container
from apps.api.container import ApiContainer
from apps.api.metering import MeteringPolicy
from apps.api.recon_snapshot import BatchSnapshot
from apps.api.recon_store import InMemoryReconStore
from apps.api.store import (
    PendingWrite,
    ReconBatchRecord,
    TicketRecord,
    TicketRepository,
    TicketStepRecord,
)
from apps.main import create_app
from apps.platform.config import Settings
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.levels import SceneSpec, WriteLevel
from apps.recon.engine import empty_result
from apps.security.identity import ScopeGrant, UserPrincipal

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

OPERATOR_A = UUID("a0000000-0000-0000-0000-000000000001")
OTHER_A = UUID("a0000000-0000-0000-0000-000000000002")
REVIEWER_A = UUID("a0000000-0000-0000-0000-000000000003")
APPROVER_A = UUID("a0000000-0000-0000-0000-000000000004")
AUDITOR_A = UUID("a0000000-0000-0000-0000-000000000005")
OPERATOR_B = UUID("b0000000-0000-0000-0000-000000000001")

#: 三条路径都要覆盖：伪造租户/组织头不得改变判定。
FORGED_HEADERS = ("X-Org-Id", "X-Tenant-Id", "X-Org")


def _principal(tenant: UUID, user_id: UUID, roles: frozenset[str]) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=tenant,
        user_id=user_id,
        roles=roles,
        grant=ScopeGrant(
            org_ids=frozenset({ORG}),
            periods=frozenset({PERIOD}),
            system_ids=frozenset({SYSTEM}),
        ),
    )


def _ticket(
    *,
    ticket_no: str,
    tenant_id: UUID,
    initiator_id: UUID,
    state: TicketState = TicketState.PENDING_APPROVAL,
    level: WriteLevel = WriteLevel.W1,
    amount: str = "1200.00",
    pending: PendingWrite | None = None,
) -> TicketRecord:
    return TicketRecord(
        ticket_no=ticket_no,
        tenant_id=tenant_id,
        scene_code=SCENE,
        initiator_id=initiator_id,
        business_key=f"BK-{ticket_no}",
        org_id=ORG,
        period=PERIOD,
        systems=(SYSTEM,),
        write_level=level,
        amount=Decimal(amount),
        touches_fund=False,
        state=state,
        created_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
        pending_write=pending,
    )


@dataclass
class IdorEnv:
    client: TestClient
    container: ApiContainer
    connector: FakeConnector
    tickets: dict[str, UUID] = field(default_factory=dict)
    batch_b: UUID | None = None
    tokens: dict[str, str] = field(default_factory=dict)

    def headers(self, who: str, idem: str | None = None, **extra: str) -> dict[str, str]:
        headers = {"X-Session-Token": self.tokens[who]}
        if idem is not None:
            headers["Idempotency-Key"] = idem
        headers.update(extra)
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


def build_env() -> IdorEnv:
    connector = FakeConnector(meta_a())
    container = ApiContainer(
        connectors={SYSTEM: connector},
        scenes={SCENE: SceneSpec(code=SCENE, max_level=WriteLevel.W2)},
        metering=MeteringPolicy(unit_price=Decimal(1)),
        recon_store=InMemoryReconStore(),
        tickets=TicketRepository(),
    )
    for action in (action_forward(), action_reverse()):
        container.actions.register(action)

    tickets = {
        # 攻击者自己的工单（对照组：同租户 + 发起人本人）。
        "own": _ticket(
            ticket_no="T-A-OWN",
            tenant_id=TENANT_A,
            initiator_id=OPERATOR_A,
            pending=PendingWrite(
                action_key="u8.trade.save_draft",
                system=SYSTEM,
                payload={"draft_id": "D-A-1", "note": "自有"},
                idem_key="idem-a-own",
            ),
        ),
        # 同租户、他人发起：用于「同租户但不属于当前用户」的审批/驳回场景。
        "other_user": _ticket(
            ticket_no="T-A-OTHER",
            tenant_id=TENANT_A,
            initiator_id=OTHER_A,
            level=WriteLevel.W2,
            amount="200000.00",
        ),
        # 跨租户待审批工单：发起人是租户 B 的用户。
        "foreign": _ticket(
            ticket_no="T-B-PENDING",
            tenant_id=TENANT_B,
            initiator_id=OPERATOR_B,
        ),
        # 跨租户且发起人 UUID 与租户 A 操作员碰撞：证明缺少租户校验时的最坏结果。
        "foreign_collide": _ticket(
            ticket_no="T-B-COLLIDE",
            tenant_id=TENANT_B,
            initiator_id=OPERATOR_A,
            pending=PendingWrite(
                action_key="u8.trade.save_draft",
                system=SYSTEM,
                payload={"draft_id": "D-B-1", "note": "跨租户碰撞"},
                idem_key="idem-b-collide",
            ),
        ),
        # 跨租户已执行工单：影像 / 链 / 导出 / 回滚的读取对象。
        "foreign_exec": _ticket(
            ticket_no="T-B-EXEC",
            tenant_id=TENANT_B,
            initiator_id=OPERATOR_B,
            state=TicketState.EXECUTING,
        ),
    }
    foreign_exec = tickets["foreign_exec"]
    foreign_exec.steps.append(
        TicketStepRecord(
            step_no=1,
            action_key="u8.trade.save_draft",
            system=SYSTEM,
            object_type="u8.trade",
            object_id="SO-B-1",
            level=WriteLevel.W1,
            idem_key="idem-b-exec",
        )
    )
    for ticket in tickets.values():
        container.tickets.add(ticket)

    asyncio.run(_seed_audit_images(container, foreign_exec))

    result = empty_result("跨租户扫描：占位批次")
    batch_b = ReconBatchRecord(
        batch_id=UUID("bbbbbbbb-0000-0000-0000-000000000001"),
        tenant_id=TENANT_B,
        org_id=ORG,
        period=PERIOD,
        system_ids=(SYSTEM,),
        suppliers=("SUP-B",),
        status=result.batch_status.value,
        conclusive=result.conclusive,
        status_reason=result.status_reason,
        metrics=None,
        missing_items=(),
        result=result,
        created_by=OPERATOR_B,
        created_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
    )
    container.recon_batches.add(batch_b)
    asyncio.run(
        container.recon_store.save(
            BatchSnapshot(
                batch_id=batch_b.batch_id,
                tenant_id=TENANT_B,
                org_id=ORG,
                period=PERIOD,
                system_ids=(SYSTEM,),
                suppliers=("SUP-B",),
                status=result.batch_status.value,
                conclusive=result.conclusive,
                status_reason=result.status_reason,
                metrics=None,
                input_fingerprint="sha256:idor-b",
                rerun_of=None,
                rerun_reason=None,
                created_by=OPERATOR_B,
                created_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
                lines=(),
            )
        )
    )

    tokens = {
        "operator": container.session_directory.issue(
            container.broker, _principal(TENANT_A, OPERATOR_A, frozenset({"operator"}))
        ),
        "reviewer": container.session_directory.issue(
            container.broker,
            _principal(TENANT_A, REVIEWER_A, frozenset({"reviewer", "finance"})),
        ),
        "approver": container.session_directory.issue(
            container.broker,
            _principal(TENANT_A, APPROVER_A, frozenset({"approver", "finance"})),
        ),
        "auditor": container.session_directory.issue(
            container.broker, _principal(TENANT_A, AUDITOR_A, frozenset({"auditor"}))
        ),
        "foreign_operator": container.session_directory.issue(
            container.broker, _principal(TENANT_B, OPERATOR_B, frozenset({"operator"}))
        ),
    }

    configure_container(container)
    app = create_app(Settings(image_tag="test"))
    return IdorEnv(
        client=TestClient(app),
        container=container,
        connector=connector,
        tickets={name: ticket.ticket_id for name, ticket in tickets.items()},
        batch_b=batch_b.batch_id,
        tokens=tokens,
    )


async def _seed_audit_images(container: ApiContainer, ticket: TicketRecord) -> None:
    step = ticket.steps[0]
    await container.ledger.append_image(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.ticket_id,
        step_id=step.step_id,
        system=step.system,
        object_type="u8.trade.save_draft",
        object_id=step.object_id,
        before={"status": "draft"},
        after={"status": "saved"},
    )


def teardown_env() -> None:
    reset_container()


__all__ = [
    "APPROVER_A",
    "AUDITOR_A",
    "FORGED_HEADERS",
    "OPERATOR_A",
    "OPERATOR_B",
    "OTHER_A",
    "PREFIX",
    "REVIEWER_A",
    "TENANT_A",
    "TENANT_B",
    "IdorEnv",
    "build_env",
    "teardown_env",
]
