"""工单事件链只读接口：顺序稳定、空链、跨租户、篡改定位、零写入。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.agents.events import (
    COMPENSATION_EVENT,
    CREATED_EVENT,
    STEP_EVENT,
    SYSTEM_ACTOR_ID,
    TRANSITION_EVENT,
    EventRecord,
    make_event,
    replay,
)
from apps.agents.state_machine import TransitionContext
from apps.agents.store import TicketStore
from apps.api import configure_container, reset_container
from apps.api.container import ApiContainer
from apps.api.ticket_sql import SqlTicketRepository
from apps.main import create_app
from apps.platform.config import Settings
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.levels import SceneSpec, WriteLevel
from apps.security.identity import ScopeGrant, UserPrincipal
from conftest import (
    INITIATOR,
    ORG,
    PERIOD,
    PREFIX,
    SCENE,
    SYSTEM,
    TENANT,
    ApiEnv,
    EmptyPriceBook,
    FakeConnector,
)

_DSN = os.environ.get("ERP_TEST_DATABASE_URL")


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _principal(tenant_id: UUID, user_id: UUID) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=frozenset({"operator"}),
        grant=ScopeGrant(
            org_ids=frozenset({ORG}),
            periods=frozenset({PERIOD}),
            system_ids=frozenset({SYSTEM}),
        ),
    )


def _get_events(env, ticket_id: UUID, who: str = "initiator"):  # type: ignore[no-untyped-def]
    return env.client.get(
        f"{PREFIX}/tickets/{ticket_id}/events",
        params=env.scope_params(),
        headers=env.headers(who),
    )


# ---------------------------------------------------------------- 内存实现


def test_ticket_without_events_returns_empty_array(env: ApiEnv) -> None:
    response = _get_events(env, env.tickets["normal"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["events"] == []
    assert body["chain"] == {
        "ok": True,
        "checked": 0,
        "broken_at": None,
        "detail": "",
        "head_hash": "0" * 64,
    }


def test_cross_tenant_cannot_read_events(env: ApiEnv) -> None:
    other = _principal(uuid4(), INITIATOR)
    token = env.container.session_directory.issue(env.container.broker, other)
    response = env.client.get(
        f"{PREFIX}/tickets/{env.tickets['normal']}/events",
        params=env.scope_params(),
        headers={"X-Session-Token": token},
    )
    assert response.status_code == 404, response.text


def test_tampered_event_chain_reports_break_position(env: ApiEnv, monkeypatch) -> None:
    ticket_id = env.tickets["normal"]
    created = make_event(
        None,
        event_type=CREATED_EVENT,
        to_state=TicketState.PENDING_APPROVAL.value,
        actor_id=str(INITIATOR),
    )
    created = replace(created, occurred_at=datetime.now(UTC))
    stepped = make_event(
        created,
        event_type=STEP_EVENT,
        payload={"step_no": 1, "state": "succeeded", "action_key": "u8.trade.save_draft"},
        actor_id=str(INITIATOR),
    )
    stepped = replace(stepped, occurred_at=datetime.now(UTC))
    tampered = replace(stepped, hash="0" * 64)  # 造一行坏 hash

    async def fake_list_events(
        *, tenant_id: UUID, ticket_id: UUID
    ) -> list[EventRecord]:
        assert ticket_id == env.tickets["normal"]
        return [created, tampered]

    monkeypatch.setattr(env.container.tickets, "list_events", fake_list_events)

    response = _get_events(env, ticket_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [event["chain_seq"] for event in body["events"]] == [1, 2]
    assert body["events"][1]["hash"] == "0" * 64
    assert body["chain"]["ok"] is False
    assert body["chain"]["broken_at"] == 2
    assert "哈希" in body["chain"]["detail"]
    assert body["chain"]["head_hash"] == "0" * 64


# ---------------------------------------------------------------- 隔离 PG


@pytest.fixture
def pg() -> Iterator[async_sessionmaker[AsyncSession]]:
    if not _DSN:
        pytest.skip("未设置 ERP_TEST_DATABASE_URL，跳过事件链 PG 用例")
    engine = create_async_engine(_DSN, poolclass=NullPool)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE erp.ticket_event_anchor, erp.ticket_event, "
                    "erp.ticket_step, erp.ticket, erp.ticket_plan_step CASCADE"
                )
            )

    _run(_clean())
    try:
        yield sf
    finally:
        _run(engine.dispose())
        reset_container()


def _build_pg(sf) -> tuple[TestClient, UUID, dict[str, str], TicketStore]:  # type: ignore[no-untyped-def]
    async def _seed() -> UUID:
        store = TicketStore(sf)
        suffix = uuid4().hex[:8]
        ticket = await store.create_ticket(
            tenant_id=TENANT,
            ticket_no=f"T-EVT-{suffix}",
            scene_code=SCENE,
            initiator_id=INITIATOR,
            trigger_kind="manual",
            business_key=f"BK-EVT-{suffix}",
            org_id=ORG,
            period=PERIOD,
            systems=(SYSTEM,),
            write_level=int(WriteLevel.W1),
            amount=Decimal("1200.00"),
            touches_fund=False,
            pending_write={
                "action_key": "u8.trade.save_draft",
                "system": SYSTEM,
                "payload": {"draft_id": f"D-{suffix}", "note": "事件链样例"},
                "idem_key": f"idem-{suffix}",
            },
        )
        await store.transition(
            ticket.id,
            TicketState.PENDING_APPROVAL,
            "preview_ok",
            context=TransitionContext(preview_ok=True),
            actor_id=INITIATOR,
        )
        return ticket.id

    ticket_id = _run(_seed())
    container = ApiContainer(
        connectors={SYSTEM: FakeConnector()},
        scenes={SCENE: SceneSpec(code=SCENE, max_level=WriteLevel.W2)},
        price_book=EmptyPriceBook(),
        tickets=SqlTicketRepository(sf),
    )
    token = container.session_directory.issue(
        container.broker, _principal(TENANT, INITIATOR)
    )
    configure_container(container)
    client = TestClient(create_app(Settings(image_tag="test")), raise_server_exceptions=False)
    return client, ticket_id, {"initiator": token}, TicketStore(sf)


@pytest.mark.skipif(not _DSN, reason="未设置 ERP_TEST_DATABASE_URL")
def test_pg_events_order_is_stable_and_readonly(pg) -> None:  # type: ignore[no-untyped-def]
    client, ticket_id, tokens, store = _build_pg(pg)
    try:
        before = _run(store.list_events(ticket_id))
        assert [event.seq for event in before] == [1, 2]

        first = client.get(
            f"{PREFIX}/tickets/{ticket_id}/events",
            params=[("org_id", ORG), ("period", PERIOD), ("system_ids", SYSTEM)],
            headers={"X-Session-Token": tokens["initiator"]},
        )
        assert first.status_code == 200, first.text
        body = first.json()

        seqs = [event["chain_seq"] for event in body["events"]]
        assert seqs == sorted(seqs) == [1, 2]
        assert body["events"][0]["event_type"] == CREATED_EVENT
        # created 的 actor 取发起人；trigger 对 created 不适用 → null，不是空串。
        assert body["events"][0]["actor"] == str(INITIATOR)
        assert body["events"][0]["trigger"] is None
        assert body["events"][1]["event_type"] == TRANSITION_EVENT
        assert body["events"][1]["from_state"] == TicketState.PLANNED.value
        assert body["events"][1]["trigger"] == "preview_ok"
        assert body["events"][1]["actor"] == str(INITIATOR)
        assert all(event["occurred_at"] for event in body["events"])
        assert body["chain"]["ok"] is True
        assert body["chain"]["broken_at"] is None
        assert body["chain"]["checked"] == 2
        assert body["chain"]["head_hash"] == body["events"][-1]["hash"]

        # 顺序稳定：重复请求返回完全一致的 JSON
        second = client.get(
            f"{PREFIX}/tickets/{ticket_id}/events",
            params=[("org_id", ORG), ("period", PERIOD), ("system_ids", SYSTEM)],
            headers={"X-Session-Token": tokens["initiator"]},
        )
        assert second.status_code == 200
        assert second.json() == body

        after = _run(store.list_events(ticket_id))
        assert len(after) == len(before)  # 只读接口零写入
    finally:
        reset_container()


def test_actor_is_not_part_of_event_hash() -> None:
    """补 actor 不改哈希：同样内容的事件，actor 为 None / 用户 / 系统时 hash 相同。"""

    payload = {"scene_code": SCENE, "business_key": "BK-HASH-1"}
    anonymous = make_event(
        None,
        event_type=CREATED_EVENT,
        to_state=TicketState.PLANNED.value,
        payload=payload,
    )
    by_user = make_event(
        None,
        event_type=CREATED_EVENT,
        to_state=TicketState.PLANNED.value,
        payload=payload,
        actor_id=str(INITIATOR),
    )
    by_system = make_event(
        None,
        event_type=CREATED_EVENT,
        to_state=TicketState.PLANNED.value,
        payload=payload,
        actor_id=str(SYSTEM_ACTOR_ID),
    )
    assert anonymous.hash == by_user.hash == by_system.hash


@pytest.mark.skipif(not _DSN, reason="未设置 ERP_TEST_DATABASE_URL")
def test_pg_compensation_actor_is_system_and_existing_hashes_unchanged(pg) -> None:  # type: ignore[no-untyped-def]
    """补偿路径 actor 是明确系统主体；补齐前后已有事件的 hash 逐字段不变（实测）。"""

    client, ticket_id, tokens, store = _build_pg(pg)
    try:
        before = _run(store.list_events(ticket_id))
        snapshot = [
            (event.seq, event.event_type, event.prev_hash, event.hash, event.computed_hash)
            for event in before
        ]
        head_before = before[-1].hash

        # 补偿 worker 是非 HTTP 路径：没有用户主体，记系统主体（不是 NULL、不是空串）
        _run(
            store.record_compensation(
                ticket_id=ticket_id, compensate_seq=1, state="compensated"
            )
        )

        after = _run(store.list_events(ticket_id))
        assert [event.seq for event in after] == [1, 2, 3]
        compensation = after[-1]
        assert compensation.event_type == COMPENSATION_EVENT
        assert compensation.actor_id == str(SYSTEM_ACTOR_ID)

        # 已有事件逐字段不变：hash / prev_hash / 重算 hash 全部相同，链仍可重放
        assert [
            (event.seq, event.event_type, event.prev_hash, event.hash, event.computed_hash)
            for event in after[:2]
        ] == snapshot
        assert after[1].hash == head_before
        assert replay(after).last_hash == compensation.hash

        response = client.get(
            f"{PREFIX}/tickets/{ticket_id}/events",
            params=[("org_id", ORG), ("period", PERIOD), ("system_ids", SYSTEM)],
            headers={"X-Session-Token": tokens["initiator"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        last = body["events"][-1]
        assert last["event_type"] == COMPENSATION_EVENT
        assert last["actor"] == str(SYSTEM_ACTOR_ID)
        assert last["trigger"] is None  # 不适用，不是空串
        assert body["chain"]["ok"] is True
        assert body["chain"]["head_hash"] == compensation.hash
    finally:
        reset_container()
