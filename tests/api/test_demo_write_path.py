"""演示写路径：真实 U8 连接器 + 本地「模拟外部系统（u8-stub）」。

与存量用例的区别：这里不用 FakeConnector，而是走 ``register_u8_connector``
（生产 ``get_container()`` 调用的同一个函数）装配真实连接器，外部副作用落在
本地桩上，用于证明四件事：

1. W2 工单审批通过后真的写入桩，并留下带 ``field_diff`` 的审计影像；
2. W3 工单被硬拒绝，且桩上没有任何请求（没有「写完再回滚」）；
3. 桩返回 5xx 时执行显式失败，不会静默成功；
4. 补偿后能从桩的 ``/reversals`` 查到冲销痕迹（可回滚的实证）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from apps.api import configure_container, reset_container
from apps.api.container import ApiContainer, register_u8_connector
from apps.api.store import PendingWrite, TicketRepository
from apps.audit.rollback import RollbackService
from apps.connectors.u8.stub_server import StubU8Server
from apps.main import create_app
from apps.platform.config import Settings
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.gateway import FieldScopeViolation, WriteGateway
from apps.platform.policy.levels import SceneSpec, WriteLevel
from apps.security.identity import ScopeGrant, UserPrincipal
from conftest import (
    APPROVER,
    INITIATOR,
    ORG,
    PERIOD,
    PREFIX,
    SCENE,
    SYSTEM,
    TENANT,
    REVIEWER,
    EmptyPriceBook,
    _ticket,
)

_PO_ACTION = "u8.po.create_draft"


def _po_payload(source_doc_no: str) -> dict:
    return {
        "source_doc_no": source_doc_no,
        "supplier_code": "S-0001",
        "doc_date": "2026-09-28",
        "lines": [
            {
                "item_code": "I-0001",
                "quantity": "10",
                "unit": "EA",
                "unit_price": "12.50",
                "tax_rate": "0.13",
            }
        ],
        "memo": "演示：写入模拟外部系统（u8-stub），不是真实 U8",
    }


@dataclass
class WriteEnv:
    client: TestClient
    stub: StubU8Server
    container: ApiContainer
    tickets: dict[str, UUID] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)

    def headers(self, who: str = "initiator", idem: str | None = None) -> dict[str, str]:
        headers = {"X-Session-Token": self.tokens[who]}
        if idem is not None:
            headers["Idempotency-Key"] = idem
        return headers

    def scope(self) -> dict:
        return {"org_id": ORG, "period": PERIOD, "system_ids": [SYSTEM]}

    def decision(self, ticket: str, *, role: str = "approver", who: str = "approver", key: str = "k-decide"):
        return self.client.post(
            f"{PREFIX}/approvals/{self.tickets[ticket]}/approve",
            json={"scope": self.scope(), "role": role, "comment": "同意（演示）"},
            headers=self.headers(who, key),
        )

    def approve_both(self, ticket: str) -> None:
        """U8 是 B 级连接器 → 强制双人复核：reviewer + approver 各签一次。"""

        reviewed = self.decision(ticket, role="reviewer", who="reviewer", key=f"k-review-{ticket}")
        assert reviewed.status_code == 200, reviewed.text
        approved = self.decision(ticket, role="approver", who="approver", key=f"k-approve-{ticket}")
        assert approved.status_code == 200, approved.text
        assert approved.json()["satisfied"] is True

    def execute(self, ticket: str, *, key: str = "k-exec"):
        return self.client.post(
            f"{PREFIX}/tickets/{self.tickets[ticket]}/actions/approve",
            json={"scope": self.scope(), "comment": "审批通过并执行"},
            headers=self.headers("initiator", key),
        )


@pytest.fixture
def write_env() -> WriteEnv:
    stub = StubU8Server().start()
    container = ApiContainer(
        connectors={},
        scenes={SCENE: SceneSpec(code=SCENE, max_level=WriteLevel.W2)},
        price_book=EmptyPriceBook(),
        tickets=TicketRepository(),
    )
    # 生产 get_container() 用同一个装配函数；这里只把 base_url 指到本地桩。
    register_u8_connector(container, base_url=stub.base_url)

    tickets = {
        "w2": _ticket(
            ticket_no="T-W2-9001",
            level=WriteLevel.W2,
            state=TicketState.PENDING_APPROVAL,
            amount="5000.00",
            pending=PendingWrite(
                action_key=_PO_ACTION,
                system=SYSTEM,
                payload=_po_payload("PO-DEMO-W2-001"),
                idem_key="idem-demo-w2",
            ),
        ),
        "w3": _ticket(
            ticket_no="T-W3-9002",
            level=WriteLevel.W3,
            state=TicketState.PENDING_APPROVAL,
            amount="100.00",
            pending=PendingWrite(
                action_key=_PO_ACTION,
                system=SYSTEM,
                payload=_po_payload("PO-DEMO-W3-001"),
                idem_key="idem-demo-w3",
            ),
        ),
        # 反向动作只允许补偿链路执行：不得被当作工单的正向写入。
        "delete_forward": _ticket(
            ticket_no="T-DEL-9003",
            level=WriteLevel.W0,
            state=TicketState.PENDING_APPROVAL,
            amount="0.00",
            pending=PendingWrite(
                action_key="u8.po.delete_draft",
                system=SYSTEM,
                payload={"cCode": "PO-STUB-DEMO-W2-001"},
                idem_key="idem-demo-delete",
            ),
        ),
    }
    for ticket in tickets.values():
        container.tickets.add(ticket)

    def principal(user_id: UUID, roles: frozenset[str]) -> UserPrincipal:
        return UserPrincipal(
            tenant_id=TENANT,
            user_id=user_id,
            roles=roles,
            grant=ScopeGrant(
                org_ids=frozenset({ORG}),
                periods=frozenset({PERIOD}),
                system_ids=frozenset({SYSTEM, "statement"}),
            ),
        )

    tokens = {
        "initiator": container.session_directory.issue(
            container.broker, principal(INITIATOR, frozenset({"operator"}))
        ),
        "reviewer": container.session_directory.issue(
            container.broker, principal(REVIEWER, frozenset({"reviewer", "finance"}))
        ),
        "approver": container.session_directory.issue(
            container.broker, principal(APPROVER, frozenset({"approver", "finance"}))
        ),
    }

    configure_container(container)
    # 平台层把未映射异常兜底成 500 + E-CONN-UNAVAILABLE；TestClient 默认会把
    # 该异常重新抛出，这里按生产行为读响应体。
    client = TestClient(create_app(Settings(image_tag="test")), raise_server_exceptions=False)
    env = WriteEnv(
        client=client,
        stub=stub,
        container=container,
        tickets={name: ticket.ticket_id for name, ticket in tickets.items()},
        tokens=tokens,
    )
    try:
        yield env
    finally:
        reset_container()
        stub.stop()


def test_w2_write_reaches_stub_and_leaves_field_diff(write_env: WriteEnv) -> None:
    write_env.approve_both("w2")

    executed = write_env.execute("w2")
    assert executed.status_code == 200, executed.text
    assert executed.json()["state"] == "执行中"

    receipts = write_env.stub.state.receipts
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["source_doc_no"] == "PO-DEMO-W2-001"
    assert receipt["cCode"].startswith("PO-STUB-")
    assert receipt["simulated"] is True

    images = write_env.client.get(
        f"{PREFIX}/audit/tickets/{write_env.tickets['w2']}/images",
        params=[("org_id", ORG), ("period", PERIOD), ("system_ids", SYSTEM)],
        headers=write_env.headers(),
    )
    assert images.status_code == 200, images.text
    items = images.json()["items"]
    assert len(items) == 1
    assert items[0]["system"] == SYSTEM
    assert items[0]["field_diff"], "审计影像必须留下 field_diff"


def test_w3_is_refused_with_zero_external_requests(write_env: WriteEnv) -> None:
    executed = write_env.execute("w3")

    assert executed.status_code == 403, executed.text
    assert executed.json()["code"] == "E-POLICY-W3FORBIDDEN"
    assert write_env.stub.state.requests == []
    assert write_env.stub.state.receipts == []


def test_stub_5xx_fails_loudly_and_can_retry(write_env: WriteEnv) -> None:
    write_env.approve_both("w2")
    write_env.stub.state.fail_posts = 10

    failed = write_env.execute("w2")
    assert failed.status_code >= 500, failed.text
    assert failed.json()["code"] == "E-CONN-UNAVAILABLE"
    assert write_env.stub.state.receipts == []

    write_env.stub.state.fail_posts = 0
    retried = write_env.execute("w2", key="k-exec-retry")
    assert retried.status_code == 200, retried.text
    assert len(write_env.stub.state.receipts) == 1


def test_compensation_is_visible_on_stub_as_reversal(write_env: WriteEnv) -> None:
    write_env.approve_both("w2")
    assert write_env.execute("w2").status_code == 200
    receipt = write_env.stub.state.receipts[0]

    compensated = write_env.client.post(
        f"{PREFIX}/tickets/{write_env.tickets['w2']}/actions/compensate",
        json={"scope": write_env.scope(), "comment": "步骤失败", "fail_code": "F1"},
        headers=write_env.headers("initiator", "k-compensate"),
    )

    assert compensated.status_code == 200, compensated.text
    assert compensated.json()["state"] == "已回滚"
    assert write_env.stub.state.receipts == []
    reversals = write_env.stub.state.reversals
    assert len(reversals) == 1
    assert reversals[0]["cCode"] == receipt["cCode"]
    assert reversals[0]["source_doc_no"] == "PO-DEMO-W2-001"
    assert reversals[0]["simulated"] is True


def test_compensation_gateway_gets_inverse_action_whitelist(
    write_env: WriteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单必须是 delete 动作的 ``cCode``，而不是正向动作的 5 个字段。"""

    write_env.approve_both("w2")
    assert write_env.execute("w2").status_code == 200

    captured: list[frozenset[str] | None] = []
    original = RollbackService.execute

    async def spy(self, plan, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(self._gateway._narrowed_fields)
        return await original(self, plan, **kwargs)

    monkeypatch.setattr(RollbackService, "execute", spy)

    compensated = write_env.client.post(
        f"{PREFIX}/tickets/{write_env.tickets['w2']}/actions/compensate",
        json={"scope": write_env.scope(), "comment": "步骤失败", "fail_code": "F1"},
        headers=write_env.headers("initiator", "k-compensate-whitelist"),
    )
    assert compensated.status_code == 200, compensated.text
    # 空集/正向字段都算错：补偿入参只有 cCode。
    assert captured == [frozenset({"cCode"})]


def test_wrong_whitelist_is_rejected_before_any_external_write(
    write_env: WriteEnv,
) -> None:
    """网关契约描述：``narrowed_fields`` 与动作字段不相交时必须零外部调用。

    这条**不承担补偿路径的防回归职责**——补偿实际传的是反向动作的 ``{cCode}``
    由 ``test_compensation_gateway_gets_inverse_action_whitelist`` 守。这里只证明
    「把正向 5 字段误传给 delete 动作」在网关层就会红，且发生在任何外部请求之前。
    """

    delete_action = write_env.container.actions.get("u8.po.delete_draft")
    forward_fields = frozenset(
        {"source_doc_no", "supplier_code", "doc_date", "lines", "memo"}
    )
    gateway = WriteGateway(
        write_env.container.connectors[SYSTEM], narrowed_fields=forward_fields
    )
    scene = SceneSpec(code=SCENE, max_level=WriteLevel.W2)

    with pytest.raises(FieldScopeViolation):
        asyncio.run(
            gateway.execute(
                delete_action,
                {"cCode": "PO-STUB-WRONG-WHITELIST"},
                ticket_id="t-demo",
                idem_key="idem-wrong-whitelist",
                scene=scene,
            )
        )
    # 校验发生在任何外部调用之前：桩上没有请求，也没有冲销。
    assert write_env.stub.state.requests == []
    assert write_env.stub.state.reversals == []


def test_compensation_only_action_cannot_be_forward_write(write_env: WriteEnv) -> None:
    """目标草稿真实存在：去掉 compensation_only 后这里会真的把它删掉。"""

    c_code = "PO-STUB-DEMO-W2-001"
    write_env.stub.state.add_receipt(
        {
            "cCode": c_code,
            "source_doc_no": c_code,
            "received_at": "2026-11-11T10:00:00+08:00",
            "simulated": True,
        }
    )

    executed = write_env.execute("delete_forward")

    assert executed.status_code == 403, executed.text
    assert executed.json()["code"] == "E-POLICY-LEVEL"
    # 零写入：草稿还在、没有冲销记录、桩上没有任何 DELETE。
    assert [r["cCode"] for r in write_env.stub.state.receipts] == [c_code]
    assert write_env.stub.state.reversals == []
    assert [r for r in write_env.stub.state.requests if r["method"] == "DELETE"] == []
