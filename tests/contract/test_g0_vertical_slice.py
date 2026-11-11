"""G0 门禁：最小纵向切片必须整条链路真的跑通。

判据取自看板 T007：「fake 连接器 → AG-1 走完 happy path → 审计有前像后像 →
前端空壳能看到工单」。本用例用真实 ``ApiContainer`` / ``WriteGateway`` / 审计账本 /
工单状态机，只把外部系统替换成记录型 ``FakeConnector``（唯一可观测副作用点）。

判别力约定：删掉任一环节都必须红——连接器没被调用、审计没落前像后像、哈希链断裂、
工单查不到，各自对应下面一条独立断言，不做「只看一个总数」的弱断言。

证据等级说明（勿升级引用）：
- 后端四段（连接器 / 工单状态 / 审计影像 / 列表接口）是**运行时证据**；
- 前端只做 ``client.ts`` / ``TicketsPage.tsx`` 的文本断言，属于**文本层证据**：
  只能证明空壳里存在这个调用串，不能证明浏览器里可用（那要 e2e/浏览器用例）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from apps.api import configure_container, reset_container
from apps.api.container import ApiContainer
from apps.api.price_book import PriceLookup
from apps.api.recon_store import FileReconStore
from apps.api.store import PendingWrite, TicketRecord, TicketRepository
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
PERIOD = "2026-11"
SYSTEM = "u8"
# AG-1（对账 Agent）当前唯一落地的场景是 A1 委外加工费对账，场景编号沿用
# 历史命名 ``ag2_processing_fee``（见 apps/security/roles.py 的类别注册表）。
SCENE = "ag2_processing_fee"
PREFIX = "/erp/api/v1"
ACTION_KEY = "u8.trade.save_draft"
IDEM_KEY = "idem-g0-1001"

INITIATOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeConnector(BaseConnector):
    """记录型替身：预演/写入/校验都可观测，写入本身不碰外部系统。"""

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


class EmptyPriceBook:
    async def effective_price(self, *, tenant_id, currency, at):
        return PriceLookup(quote=None, has_other_currency_price=False)


def _action() -> ActionSpec:
    return ActionSpec(
        key=ACTION_KEY,
        level=WriteLevel.W1,
        writable_fields=frozenset({"draft_id", "note"}),
        compensation=CompensateSpec(
            kind="delete_draft", action_key="u8.trade.delete_draft"
        ),
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
            system_ids=frozenset({SYSTEM}),
        ),
    )


def _scope() -> dict[str, Any]:
    return {"org_id": ORG, "period": PERIOD, "system_ids": [SYSTEM]}


def _scope_params() -> list[tuple[str, str]]:
    return [("org_id", ORG), ("period", PERIOD), ("system_ids", SYSTEM)]


def test_g0_gate_fake_connector_to_visible_ticket(tmp_path: Path) -> None:
    connector = FakeConnector()
    container = ApiContainer(
        connectors={SYSTEM: connector},
        scenes={SCENE: SceneSpec(code=SCENE, max_level=WriteLevel.W1)},
        recon_store=FileReconStore(tmp_path / "recon-store.json"),
        price_book=EmptyPriceBook(),
        tickets=TicketRepository(),
    )
    container.actions.register(_action())

    ticket = TicketRecord(
        ticket_no="G0-1001",
        tenant_id=TENANT,
        scene_code=SCENE,
        initiator_id=INITIATOR,
        business_key="BK-G0-1001",
        org_id=ORG,
        period=PERIOD,
        systems=(SYSTEM,),
        write_level=WriteLevel.W1,
        amount=Decimal("1200.00"),
        touches_fund=False,
        state=TicketState.PENDING_APPROVAL,
        created_at=datetime(2026, 11, 11, 9, 0, 0, tzinfo=UTC),
        pending_write=PendingWrite(
            action_key=ACTION_KEY,
            system=SYSTEM,
            payload={"draft_id": "D-G0-1", "note": "G0 切片"},
            idem_key=IDEM_KEY,
        ),
    )
    container.tickets.add(ticket)
    ticket_id = str(ticket.ticket_id)

    tokens = {
        "initiator": container.session_directory.issue(
            container.broker, _principal(INITIATOR, frozenset({"operator"}))
        ),
        "reviewer": container.session_directory.issue(
            container.broker, _principal(REVIEWER, frozenset({"reviewer", "finance"}))
        ),
    }

    def headers(who: str, idem: str) -> dict[str, str]:
        return {"X-Session-Token": tokens[who], "Idempotency-Key": idem}

    configure_container(container)
    try:
        app = create_app(Settings(image_tag="test"))
        with TestClient(app) as client:
            # 1) 审批 → 执行：真实四道闸 + WriteGateway + AG-1 场景。
            decided = client.post(
                f"{PREFIX}/approvals/{ticket_id}/approve",
                json={"scope": _scope(), "role": "reviewer", "comment": "同意"},
                headers=headers("reviewer", "k-g0-review"),
            )
            assert decided.status_code == 200, decided.text

            executed = client.post(
                f"{PREFIX}/tickets/{ticket_id}/actions/approve",
                json={"scope": _scope(), "comment": "执行"},
                headers=headers("initiator", "k-g0-exec"),
            )
            assert executed.status_code == 200, executed.text

            # 2) fake 连接器确实被调用：对象集合 + 幂等键 + 载荷，不看次数了事。
            assert {write[0] for write in connector.writes} == {ACTION_KEY}, (
                "G0 断链：WriteGateway 没有把 AG-1 的写动作交给连接器"
            )
            assert len(connector.writes) == 1, f"写动作应恰好一次，实际 {connector.writes}"
            _, payload, written_ticket, idem_key = connector.writes[0]
            assert written_ticket == ticket_id
            assert idem_key == IDEM_KEY
            assert payload == {"draft_id": "D-G0-1", "note": "G0 切片"}

            # 3) 工单状态推进（列表/详情是前端空壳读取的同一份后端契约）。
            detail = client.get(
                f"{PREFIX}/tickets/{ticket_id}",
                params=_scope_params(),
                headers=headers("initiator", "k-g0-detail"),
            )
            assert detail.status_code == 200, detail.text
            detail_body = detail.json()
            assert detail_body["ticket_no"] == "G0-1001"
            assert detail_body["state"] == TicketState.EXECUTING.value
            assert detail_body["audit_image_count"] == 1

            # 4) 审计：断言那一条影像的全部关键字段，而不是只数条数。
            images = client.get(
                f"{PREFIX}/audit/tickets/{ticket_id}/images",
                params=_scope_params() + [("system", SYSTEM)],
                headers=headers("initiator", "k-g0-images"),
            )
            assert images.status_code == 200, images.text
            items = images.json()["items"]
            assert len(items) == 1, f"执行后应恰有一条审计影像，实际 {len(items)}"
            image = items[0]
            assert {item["object_type"] for item in items} == {ACTION_KEY}
            assert {item["ticket_id"] for item in items} == {ticket_id}
            assert image["step_id"], "审计影像必须挂在具体步骤上"
            assert image["before_ref"], "G0 断链：审计缺少前像引用"
            assert image["after_ref"], "G0 断链：审计缺少后像引用"
            assert image["diff_missing"] is False, (
                "G0 断链：前像/后像未成对送达，字段 diff 被标成缺失"
            )
            assert len(image["hash"]) == 64, image["hash"]
            assert image["prev_hash"] == "", "首条影像应是租户链的创世记录"

            chain = client.get(
                f"{PREFIX}/audit/tickets/{ticket_id}/chain",
                params=_scope_params(),
                headers=headers("initiator", "k-g0-chain"),
            )
            assert chain.status_code == 200, chain.text
            chain_body = chain.json()
            assert chain_body["ok"] is True
            assert chain_body["image_count"] == 1
            assert chain_body["head_hash"] == image["hash"]
            assert chain_body["detail"] == ""

            # 5) 列表接口：前端空壳「能看到工单」的后端证据。
            listed = client.get(
                f"{PREFIX}/tickets",
                params=_scope_params() + [("ticket_no", "G0-1001")],
                headers=headers("initiator", "k-g0-list"),
            )
            assert listed.status_code == 200, listed.text
            listed_items = listed.json()["items"]
            assert [item["ticket_no"] for item in listed_items] == ["G0-1001"]
            assert {item["ticket_id"] for item in listed_items} == {ticket_id}
            assert {item["state"] for item in listed_items} == {
                TicketState.EXECUTING.value
            }
    finally:
        reset_container()

    # 6) 前端空壳：文本层证据，仅证明调用串存在，不证明浏览器可用。
    client_ts = (REPO_ROOT / "web/src/api/client.ts").read_text(encoding="utf-8")
    assert "TICKETS_PATH = '/v1/tickets'" in client_ts, (
        "证据等级=文本层：空壳的 HTTP 客户端未指向本用例已验证的 /v1/tickets"
    )
    tickets_page = (REPO_ROOT / "web/src/pages/TicketsPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "api.listTickets(" in tickets_page, (
        "证据等级=文本层：TicketsPage 未调用 listTickets（不能据此声称前端已打通）"
    )
    assert "ticket.ticket_no" in tickets_page, (
        "证据等级=文本层：TicketsPage 未渲染工单号（不能据此声称前端已打通）"
    )
