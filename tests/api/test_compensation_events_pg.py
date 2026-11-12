"""补偿事件化（B-COMP-NO-EVENT）：ticket_event 哈希链、幂等、关闭态零副作用。

只跑在 ``ERP_TEST_DATABASE_URL`` 指向的隔离 PG 上；不碰 live 演示栈。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.agents.audit_guard import AuditGuard
from apps.agents.events import COMPENSATION_EVENT
from apps.agents.state_machine import TransitionContext
from apps.agents.store import TicketStore
from apps.api import configure_container, reset_container
from apps.api.container import ApiContainer
from apps.api.rollback_gate import ENV_ROLLBACK_SYNC_ENABLED
from apps.api.ticket_sql import SqlTicketRepository
from apps.main import create_app
from apps.platform.config import Settings
from apps.platform.contracts.errors import ErrorCode
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.levels import SceneSpec, WriteLevel
from apps.security.identity import ScopeGrant, UserPrincipal
from conftest import (
    APPROVER,
    INITIATOR,
    ORG,
    PERIOD,
    PREFIX,
    REVIEWER,
    SCENE,
    SYSTEM,
    TENANT,
    EmptyPriceBook,
    FakeConnector,
    _forward_action,
    _reverse_action,
)

_DSN = os.environ.get("ERP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="未设置 ERP_TEST_DATABASE_URL，跳过补偿事件 PG 用例"
)

_FORWARD = "u8.trade.save_draft"


class CountingConnector(FakeConnector):
    """记录反向写：补偿在 API 侧走 ``gateway.execute(反向动作)``，不是 ``compensate``。"""

    def __init__(self) -> None:
        super().__init__()
        self.reverse_writes: list[tuple] = []

    async def write(  # type: ignore[no-untyped-def]
        self, action, payload, *, ticket_id: str, idem_key: str
    ):
        if action.key == "u8.trade.delete_draft":
            self.reverse_writes.append((action.key, dict(payload), ticket_id, idem_key))
        return await super().write(
            action, payload, ticket_id=ticket_id, idem_key=idem_key
        )


class FailingReverseConnector(CountingConnector):
    async def write(  # type: ignore[no-untyped-def]
        self, action, payload, *, ticket_id: str, idem_key: str
    ):
        if action.key == "u8.trade.delete_draft":
            raise RuntimeError("测试注入：反向写入失败")
        return await super().write(
            action, payload, ticket_id=ticket_id, idem_key=idem_key
        )


@dataclass
class Env:
    client: TestClient
    container: ApiContainer
    connector: CountingConnector
    ticket_id: UUID
    tokens: dict[str, str]

    def headers(self, who: str = "initiator", idem: str | None = None) -> dict[str, str]:
        headers = {"X-Session-Token": self.tokens[who]}
        if idem is not None:
            headers["Idempotency-Key"] = idem
        return headers

    def scope(self) -> dict:
        return {"org_id": ORG, "period": PERIOD, "system_ids": [SYSTEM]}


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


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


async def _truncate(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE erp.ticket_event_anchor, erp.ticket_event, "
                "erp.ticket_step, erp.ticket, erp.ticket_plan_step CASCADE"
            )
        )


async def _seed(sf) -> UUID:  # type: ignore[no-untyped-def]
    store = TicketStore(sf)
    suffix = uuid4().hex[:8]
    ticket = await store.create_ticket(
        tenant_id=TENANT,
        ticket_no=f"T-COMP-{suffix}",
        scene_code=SCENE,
        initiator_id=INITIATOR,
        trigger_kind="manual",
        business_key=f"BK-COMP-{suffix}",
        org_id=ORG,
        period=PERIOD,
        systems=(SYSTEM,),
        write_level=int(WriteLevel.W1),
        amount=Decimal("1200.00"),
        touches_fund=False,
        pending_write={
            "action_key": _FORWARD,
            "system": SYSTEM,
            "payload": {"draft_id": f"D-{suffix}", "note": "补偿事件演示"},
            "idem_key": f"idem-{suffix}",
        },
    )
    await store.transition(
        ticket.id,
        TicketState.PENDING_APPROVAL,
        "preview_ok",
        context=TransitionContext(preview_ok=True),
    )
    return ticket.id


def _build(sf) -> Env:  # type: ignore[no-untyped-def]
    connector = CountingConnector()
    container = ApiContainer(
        connectors={SYSTEM: connector},
        scenes={SCENE: SceneSpec(code=SCENE, max_level=WriteLevel.W2)},
        price_book=EmptyPriceBook(),
        tickets=SqlTicketRepository(sf),
    )
    container.actions.register(_reverse_action())
    container.actions.register(_forward_action())
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
    }
    configure_container(container)
    client = TestClient(create_app(Settings(image_tag="test")), raise_server_exceptions=False)
    return Env(
        client=client,
        container=container,
        connector=connector,
        ticket_id=_run(_seed(sf)),
        tokens=tokens,
    )


def _approve_and_execute(env: Env) -> None:
    approved = env.client.post(
        f"{PREFIX}/approvals/{env.ticket_id}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "同意"},
        headers=env.headers("reviewer", "k-comp-review"),
    )
    assert approved.status_code == 200, approved.text

    executed = env.client.post(
        f"{PREFIX}/tickets/{env.ticket_id}/actions/approve",
        json={"scope": env.scope(), "comment": "审批通过并执行"},
        headers=env.headers("initiator", "k-comp-exec"),
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["state"] == TicketState.EXECUTING.value


def _compensate(env: Env, key: str = "k-comp-rollback"):
    return env.client.post(
        f"{PREFIX}/tickets/{env.ticket_id}/actions/compensate",
        json={"scope": env.scope(), "comment": "步骤失败", "fail_code": "F1"},
        headers=env.headers("initiator", key),
    )


@pytest.fixture
def pg() -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_DSN or "", poolclass=NullPool)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    _run(_truncate(engine))
    try:
        yield sf
    finally:
        _run(engine.dispose())
        reset_container()


def test_compensation_writes_events_and_chain_is_clean(pg) -> None:
    env = _build(pg)
    try:
        _approve_and_execute(env)
        assert env.connector.reverse_writes == []
        events_before = _run(TicketStore(pg).list_events(env.ticket_id))

        response = _compensate(env)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == TicketState.ROLLED_BACK.value
        assert len(response.json()["executed_steps"]) == 1
        assert len(env.connector.reverse_writes) == 1  # 外部冲销只发生一次

        events = _run(TicketStore(pg).list_events(env.ticket_id))
        assert len(events) == len(events_before) + 2  # 进入补偿 + 已回滚
        to_states = [event.to_state for event in events]
        assert to_states.count(TicketState.COMPENSATING.value) == 1
        assert to_states.count(TicketState.ROLLED_BACK.value) == 1
        assert all(event.seq == index for index, event in enumerate(events, start=1))

        report = _run(AuditGuard(TicketStore(pg)).verify_all(ticket_ids=[env.ticket_id]))
        assert report.ok, report.broken
        assert report.broken == ()
    finally:
        reset_container()


def test_repeat_compensation_is_rejected_without_new_events(pg) -> None:
    env = _build(pg)
    try:
        _approve_and_execute(env)
        first = _compensate(env)
        assert first.status_code == 200, first.text
        events_after_first = _run(TicketStore(pg).list_events(env.ticket_id))
        reverse_after_first = len(env.connector.reverse_writes)

        second = _compensate(env, key="k-comp-rollback-again")
        assert second.status_code == 409, second.text
        assert second.json()["code"] == ErrorCode.STATE_ILLEGAL.value

        assert len(_run(TicketStore(pg).list_events(env.ticket_id))) == len(
            events_after_first
        )
        assert len(env.connector.reverse_writes) == reverse_after_first
    finally:
        reset_container()


def test_disabled_switch_stays_503_with_zero_events_and_zero_external_writes(
    pg, monkeypatch
) -> None:
    env = _build(pg)
    try:
        _approve_and_execute(env)
        events_before = _run(TicketStore(pg).list_events(env.ticket_id))
        writes_before = len(env.connector.writes)

        monkeypatch.delenv(ENV_ROLLBACK_SYNC_ENABLED, raising=False)
        response = _compensate(env)

        assert response.status_code == 503, response.text
        assert response.json()["code"] == ErrorCode.ROLLBACK_UNAVAILABLE.value
        assert len(_run(TicketStore(pg).list_events(env.ticket_id))) == len(events_before)
        assert len(env.connector.writes) == writes_before
        assert env.connector.reverse_writes == []
    finally:
        reset_container()


def test_compensation_exception_leaves_failed_event(pg) -> None:
    env = _build(pg)
    env.container.connectors[SYSTEM] = FailingReverseConnector()
    try:
        _approve_and_execute(env)  # 正向写仍走 FakeConnector，只有反向动作失败
        response = _compensate(env)
        assert response.status_code >= 500, response.text

        events = _run(TicketStore(pg).list_events(env.ticket_id))
        failed = [
            event
            for event in events
            if event.event_type == COMPENSATION_EVENT
            and event.payload.get("state") == "failed"
        ]
        assert len(failed) == 1
        assert TicketState.ROLLED_BACK.value not in {event.to_state for event in events}

        stored = _run(
            SqlTicketRepository(pg).get(tenant_id=TENANT, ticket_id=env.ticket_id)
        )
        assert stored is not None
        assert stored.state is TicketState.COMPENSATING

        report = _run(AuditGuard(TicketStore(pg)).verify_all(ticket_ids=[env.ticket_id]))
        assert report.ok, report.broken
    finally:
        reset_container()
