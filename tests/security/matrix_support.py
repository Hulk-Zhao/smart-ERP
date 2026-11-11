"""权限矩阵测试夹具：真实 API + AccessGate，只替换连接器与内存仓储。

与 ``tests/api/conftest.py`` 刻意分开：矩阵测试需要按「角色 × 操作 × 资源归属」
组合出 105 个用例，夹具必须在每个用例里都是干净的（工单状态会被审批/回滚推进），
且要额外提供审计员、平台管理员、服务账号三类身份与 B 级连接器。

文件职责：
- 角色/资源常量与 ``MatrixEnv``（token 目录、工单索引、可观测连接器）；
- 工单种子：同一逻辑资源派生「本组织 / 跨组织 / 跨期间（跨批次）」三份；
- 审批与审计影像种子：让回滚、看影像、导出三类用例有真实对象可读。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from apps.api import configure_container, reset_container
from apps.api.container import ApiContainer
from apps.api.store import (
    PendingWrite,
    TicketRecord,
    TicketRepository,
    TicketStepRecord,
)
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
ORG2 = "ORG-2"
PERIOD = "2026-10"
PERIOD2 = "2026-09"
SYSTEM = "u8"
SYSTEM_B = "mes"
SCENE = "ag2_processing_fee"
PREFIX = "/erp/api/v1"

INITIATOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
APPROVER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
AUDITOR = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
ADMIN = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
SERVICE = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

ROLES = ("initiator", "approver", "auditor", "admin", "service_account")
OPS = ("read", "write", "approve", "reject", "rollback", "export", "images")
OWNERSHIPS = ("same_org", "cross_org", "cross_period")

ROLE_NAMES: dict[str, str] = {
    "initiator": "发起人",
    "approver": "审批人",
    "auditor": "审计员",
    "admin": "平台管理员",
    "service_account": "服务账号",
}
OP_NAMES: dict[str, str] = {
    "read": "读工单",
    "write": "写工单",
    "approve": "审批",
    "reject": "驳回",
    "rollback": "回滚",
    "export": "导出审计",
    "images": "看影像",
}
OWNERSHIP_NAMES: dict[str, str] = {
    "same_org": "本组织",
    "cross_org": "跨组织",
    "cross_period": "跨批次",
}


class FakeConnector(BaseConnector):
    """可观测替身：记录预演/写入/校验；写入不产生真实副作用。"""

    def __init__(self, meta: ConnectorMeta) -> None:
        super().__init__()
        self.meta = meta
        self.previews: list[tuple[str, Mapping[str, Any]]] = []
        self.writes: list[tuple[str, Mapping[str, Any], str, str]] = []
        self.verifies: list[str] = []

    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("draft", "work_order"), actions=())

    async def read(self, entity: str, query: ReadQuery) -> ReadResult:
        return ReadResult(items=(), completeness="complete")

    async def preview(
        self, action: ActionSpec, payload: Mapping[str, Any]
    ) -> PreviewResult:
        self.previews.append((action.key, dict(payload)))
        return PreviewResult(
            diff=(),
            before={"object": payload.get("draft_id") or payload.get("wo_id")},
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
            response={"status": "ok"},
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


def meta_a() -> ConnectorMeta:
    return ConnectorMeta(
        system=SYSTEM,
        version="1.0.0",
        access_level="L2",
        security_level="A",
        supports_obo=True,
        version_fingerprint="sha256:matrix-u8",
    )


def meta_b() -> ConnectorMeta:
    """客户系统不支持委派 → 代码推导为 B 级（服务账号 + 收窄字段 + 强制双人）。"""

    return ConnectorMeta(
        system=SYSTEM_B,
        version="1.0.0",
        access_level="L2",
        security_level="B",
        supports_obo=False,
        version_fingerprint="sha256:matrix-mes",
    )


def action_forward() -> ActionSpec:
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


def action_reverse() -> ActionSpec:
    return ActionSpec(
        key="u8.trade.delete_draft",
        level=WriteLevel.W0,
        writable_fields=frozenset({"draft_id"}),
        verify=VerifySpec(kind="read_by_external_id", fields=("draft_id",)),
        rate_limit=RateLimit(),
    )


def action_b_forward() -> ActionSpec:
    return ActionSpec(
        key="mes.wo.save",
        level=WriteLevel.W1,
        writable_fields=frozenset({"draft_id", "note"}),
        compensation=CompensateSpec(kind="cancel_request", action_key="mes.wo.cancel"),
        verify=VerifySpec(kind="read_by_external_id", fields=("draft_id",)),
        rate_limit=RateLimit(),
    )


def action_b_reverse() -> ActionSpec:
    return ActionSpec(
        key="mes.wo.cancel",
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
        # 授权范围只含本组织本期间；越权用例靠「请求/资源」越界触发拒绝。
        grant=ScopeGrant(
            org_ids=frozenset({ORG}),
            periods=frozenset({PERIOD}),
            system_ids=frozenset({SYSTEM, SYSTEM_B}),
        ),
    )


def _ticket(
    *,
    ticket_no: str,
    kind: str,
    ownership: str,
    level: WriteLevel = WriteLevel.W1,
    state: TicketState = TicketState.PENDING_APPROVAL,
    amount: str = "1200.00",
    initiator_id: UUID = INITIATOR,
    pending: PendingWrite | None = None,
    systems: tuple[str, ...] = (SYSTEM,),
) -> TicketRecord:
    org_id, period = {
        "same_org": (ORG, PERIOD),
        "cross_org": (ORG2, PERIOD),
        "cross_period": (ORG, PERIOD2),
    }[ownership]
    return TicketRecord(
        ticket_no=ticket_no,
        tenant_id=TENANT,
        scene_code=SCENE,
        initiator_id=initiator_id,
        business_key=f"BK-{kind}-{ownership}",
        org_id=org_id,
        period=period,
        systems=systems,
        write_level=level,
        amount=Decimal(amount),
        touches_fund=False,
        state=state,
        created_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
        pending_write=pending,
    )


def _pending(system: str, ticket_no: str, action_key: str) -> PendingWrite:
    return PendingWrite(
        action_key=action_key,
        system=system,
        payload={"draft_id": f"D-{ticket_no}", "note": "矩阵用例"},
        idem_key=f"idem-{ticket_no}",
    )


@dataclass
class MatrixEnv:
    client: TestClient
    container: ApiContainer
    connector: FakeConnector
    connector_b: FakeConnector
    tickets: dict[tuple[str, str], UUID]
    tokens: dict[str, str] = field(default_factory=dict)

    def headers(self, role: str, idem: str | None = None) -> dict[str, str]:
        headers = {"X-Session-Token": self.tokens[role]}
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

    def ticket(self, kind: str, ownership: str) -> UUID:
        return self.tickets[(kind, ownership)]


def _seed_container() -> tuple[ApiContainer, FakeConnector, FakeConnector, dict[str, TicketRecord]]:
    connector = FakeConnector(meta_a())
    connector_b = FakeConnector(meta_b())
    container = ApiContainer(
        connectors={SYSTEM: connector, SYSTEM_B: connector_b},
        scenes={SCENE: SceneSpec(code=SCENE, max_level=WriteLevel.W2)},
        tickets=TicketRepository(),
    )
    for action in (
        action_forward(),
        action_reverse(),
        action_b_forward(),
        action_b_reverse(),
    ):
        container.actions.register(action)

    tickets: dict[tuple[str, str], TicketRecord] = {}
    for ownership in OWNERSHIPS:
        suffix = "" if ownership == "same_org" else f"-{ownership}"
        # 读：普通待审批工单（无写计划）。
        tickets[("read", ownership)] = _ticket(
            ticket_no=f"T-RD{suffix}",
            kind="read",
            ownership=ownership,
            amount="300.00",
        )
        # 写：待审批 + 已锁定的写计划（发起人领取后执行）。
        tickets[("write", ownership)] = _ticket(
            ticket_no=f"T-WR{suffix}",
            kind="write",
            ownership=ownership,
            pending=_pending(SYSTEM, f"T-WR{suffix}", "u8.trade.save_draft"),
        )
        # 审批：W2 大额 → 需要 reviewer + approver 两个不同审批人。
        tickets[("dual", ownership)] = _ticket(
            ticket_no=f"T-AP{suffix}",
            kind="dual",
            ownership=ownership,
            level=WriteLevel.W2,
            amount="200000.00",
            pending=_pending(SYSTEM, f"T-AP{suffix}", "u8.trade.save_draft"),
        )
        # 驳回：同样是 W2 大额，单独一份避免与审批用例互相推进状态。
        tickets[("dual_reject", ownership)] = _ticket(
            ticket_no=f"T-RJ{suffix}",
            kind="dual_reject",
            ownership=ownership,
            level=WriteLevel.W2,
            amount="200000.00",
        )
        # 回滚/影像/导出：已执行且带一条审计影像的工单。
        ticket_no = f"T-RB{suffix}"
        rolled = _ticket(
            ticket_no=ticket_no,
            kind="rolled",
            ownership=ownership,
            state=TicketState.EXECUTING,
            amount="1200.00",
        )
        rolled.steps.append(
            TicketStepRecord(
                step_no=1,
                action_key="u8.trade.save_draft",
                system=SYSTEM,
                object_type="u8.trade",
                object_id=f"SO{suffix or '-1'}",
                level=WriteLevel.W1,
                idem_key=f"idem-{ticket_no}",
            )
        )
        tickets[("rolled", ownership)] = rolled

    # W3：任何角色、任何路径都不得执行。
    tickets[("w3", "same_org")] = _ticket(
        ticket_no="T-W3",
        kind="w3",
        ownership="same_org",
        level=WriteLevel.W3,
        amount="100.00",
        pending=_pending(SYSTEM, "T-W3", "u8.trade.save_draft"),
    )
    # B 级（服务账号降级）工单：发起人即服务账号身份。
    tickets[("b_level", "same_org")] = _ticket(
        ticket_no="T-B",
        kind="b_level",
        ownership="same_org",
        amount="880.00",
        initiator_id=SERVICE,
        pending=_pending(SYSTEM_B, "T-B", "mes.wo.save"),
        systems=(SYSTEM_B,),
    )

    for ticket in tickets.values():
        container.tickets.add(ticket)

    # 审批种子：写/回滚两类工单已由复核人签字，否则发起人执行会被审批闸拦下。
    reviewer = _principal(REVIEWER, frozenset({"reviewer"}))
    for (kind, _ownership), ticket in tickets.items():
        if kind in {"write", "rolled"}:
            container.approvals.record(
                ticket_id=str(ticket.ticket_id),
                role="reviewer",
                approver=reviewer,
                initiator_id=ticket.initiator_id,
                decision="approved",
                comment="矩阵种子：复核通过",
            )

    # 审计影像种子：回滚需要原始写入影像；导出/看影像需要真实内容。
    rolled_rows = [row for (kind, _), row in tickets.items() if kind == "rolled"]
    asyncio.run(_append_images(container, rolled_rows))
    return container, connector, connector_b, tickets


async def _append_images(container: ApiContainer, rows: list[TicketRecord]) -> None:
    for ticket in rows:
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


def build_env() -> MatrixEnv:
    container, connector, connector_b, tickets = _seed_container()

    tokens = {
        "initiator": container.session_directory.issue(
            container.broker, _principal(INITIATOR, frozenset({"operator"}))
        ),
        "reviewer": container.session_directory.issue(
            container.broker, _principal(REVIEWER, frozenset({"reviewer"}))
        ),
        "approver": container.session_directory.issue(
            # 矩阵工单全部是 ag2_processing_fee（finance 类）：审批人必须持有
            # 该业务类别角色，否则业务类别闸会拒绝其落票（这正是 §4.2 的语义）。
            container.broker, _principal(APPROVER, frozenset({"approver", "finance"}))
        ),
        "auditor": container.session_directory.issue(
            container.broker, _principal(AUDITOR, frozenset({"auditor"}))
        ),
        "admin": container.session_directory.issue(
            container.broker, _principal(ADMIN, frozenset({"platform_admin"}))
        ),
        "service_account": container.session_directory.issue(
            container.broker, _principal(SERVICE, frozenset({"service_account"}))
        ),
    }

    configure_container(container)
    app = create_app(Settings(image_tag="test"))
    client = TestClient(app)
    return MatrixEnv(
        client=client,
        container=container,
        connector=connector,
        connector_b=connector_b,
        tickets={key: ticket.ticket_id for key, ticket in tickets.items()},
        tokens=tokens,
    )


def teardown_env() -> None:
    reset_container()


__all__ = [
    "ADMIN",
    "APPROVER",
    "AUDITOR",
    "INITIATOR",
    "OPS",
    "OP_NAMES",
    "ORG",
    "ORG2",
    "OWNERSHIPS",
    "OWNERSHIP_NAMES",
    "PERIOD",
    "PERIOD2",
    "PREFIX",
    "REVIEWER",
    "ROLES",
    "ROLE_NAMES",
    "SCENE",
    "SERVICE",
    "SYSTEM",
    "SYSTEM_B",
    "TENANT",
    "MatrixEnv",
    "build_env",
    "meta_a",
    "meta_b",
    "teardown_env",
]
