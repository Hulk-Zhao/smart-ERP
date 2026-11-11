"""PR3：API 写路径事件化（读库 + 写事件一起切）。

三条要钉死的事实：

1. ``version`` 对外「建单即 1」——直接断言首次 GET 的响应，不靠推理；
2. 审批（approve）不是直写投影，而是 ``TicketStore.transition``：事件链里必须
   同时看到 created → pending_approval → step → executing，且哈希链可重放；
3. ``rebuild_projection`` 能重建 API 写的投影字段（org/period/systems/
   write_level/amount/pending_write），与在线投影逐字段相等。

跳过策略与 ``test_ticket_repo_sql.py`` 一致：未设置 ``ERP_TEST_DATABASE_URL`` 时
整组 skip；CI 的 PG15 service 会设置该变量。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import conftest as cf
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.agents.state_machine import TransitionContext
from apps.agents.store import TicketStore
from apps.api.store import PendingWrite, TicketRecord
from apps.api.ticket_sql import SqlTicketRepository
from apps.platform.contracts.ticket import TicketState
from apps.platform.db.models import Ticket
from apps.platform.policy.levels import WriteLevel

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TENANT = cf.TENANT
INITIATOR = cf.INITIATOR
ORG = cf.ORG
PERIOD = cf.PERIOD
SCENE = cf.SCENE
SYSTEM = cf.SYSTEM


def _upgrade(url: str) -> None:
    config = Config(os.path.join(ROOT, "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", os.path.join(ROOT, "db", "migrations"))
    command.upgrade(config, "head")


async def _wait_ready(url: str, attempts: int = 60) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        for _ in range(attempts):
            try:
                async with engine.connect() as conn:
                    await conn.execute(sa.text("SELECT 1"))
                return
            except Exception:  # noqa: BLE001 - 等库起来期间任何连接错误都重试
                await asyncio.sleep(1)
    finally:
        await engine.dispose()
    raise RuntimeError("测试库在 60 秒内没有就绪")


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("ERP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("未设置 ERP_TEST_DATABASE_URL，跳过 PG 集成用例")
    asyncio.run(_wait_ready(url))
    _upgrade(url)
    return url


@pytest.fixture()
def session_factory(pg_url: str):
    engine = create_async_engine(pg_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean() -> None:
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE erp.ticket CASCADE"))

    asyncio.run(_clean())
    try:
        yield factory
    finally:
        asyncio.run(engine.dispose())


def _ticket(
    *,
    ticket_no: str,
    level: WriteLevel = WriteLevel.W1,
    amount: str = "1200.00",
    pending: PendingWrite | None = None,
    systems: tuple[str, ...] | None = (SYSTEM,),
) -> TicketRecord:
    return TicketRecord(
        ticket_no=ticket_no,
        tenant_id=TENANT,
        scene_code=SCENE,
        initiator_id=INITIATOR,
        business_key=f"BK-{ticket_no}",
        org_id=ORG,
        period=PERIOD,
        systems=systems,
        write_level=level,
        amount=Decimal(amount),
        touches_fund=False,
        created_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
        pending_write=pending,
    )


def _pending() -> PendingWrite:
    return PendingWrite(
        action_key="u8.trade.save_draft",
        system=SYSTEM,
        payload={"draft_id": "D-P3", "note": "PR3"},
        idem_key="idem-p3",
    )


def _stored_version(session_factory, ticket_id: UUID) -> int:
    async def _read() -> int:
        async with session_factory() as session:
            value = await session.scalar(
                sa.select(Ticket.version).where(Ticket.id == ticket_id)
            )
        assert value is not None
        return int(value)

    return asyncio.run(_read())


def test_first_api_response_version_is_one(env, session_factory):
    repo = SqlTicketRepository(session_factory=session_factory)
    env.container.tickets = repo
    created = asyncio.run(repo.create(_ticket(ticket_no="T-P3-0001")))

    response = env.client.get(
        f"{cf.PREFIX}/tickets/{created.ticket_id}",
        params=env.scope_params(),
        headers=env.headers(),
    )

    assert response.status_code == 200, response.text
    # 对外契约：建单即 1。DB 里存 0（迁移次数），偏移只发生在端口边界。
    assert response.json()["version"] == 1
    assert _stored_version(session_factory, created.ticket_id) == 0


def test_approve_writes_event_chain_not_projection_directly(env, session_factory):
    repo = SqlTicketRepository(session_factory=session_factory)
    env.container.tickets = repo
    created = asyncio.run(
        repo.create(_ticket(ticket_no="T-P3-0002", pending=_pending()))
    )
    asyncio.run(
        repo.transition(
            tenant_id=TENANT,
            ticket_id=created.ticket_id,
            target=TicketState.PENDING_APPROVAL,
            trigger="preview_ok",
            context=TransitionContext(preview_ok=True),
        )
    )

    decision = env.client.post(
        f"{cf.PREFIX}/approvals/{created.ticket_id}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "同意"},
        headers=env.headers("reviewer", "k-p3-review"),
    )
    assert decision.status_code == 200, decision.text

    response = env.client.post(
        f"{cf.PREFIX}/tickets/{created.ticket_id}/actions/approve",
        json={"scope": env.scope(), "comment": "PR3 事件化审批"},
        headers=env.headers("initiator", "k-p3-approve"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == TicketState.EXECUTING.value
    # created(1) → pending_approval(2) → approve(3)
    assert body["version"] == 3
    assert len(body["executed_steps"]) == 1
    assert len(env.connector.writes) == 1

    store = TicketStore(session_factory)
    events = asyncio.run(store.list_events(created.ticket_id))
    assert [event.seq for event in events] == [1, 2, 3, 4]
    assert [event.event_type for event in events] == [
        "created",
        "transition",
        "step",
        "transition",
    ]

    projection = asyncio.run(store.rebuild_projection(created.ticket_id))
    assert projection.state == TicketState.EXECUTING
    assert projection.version == 2  # 事件里的迁移次数；对外 +1
    assert projection.steps == {1: "succeeded"}
    assert projection.pending_write is None
    assert asyncio.run(store.check_projection(created.ticket_id)).consistent


def test_rebuild_projection_matches_online_for_api_written_fields(session_factory):
    repo = SqlTicketRepository(session_factory=session_factory)
    store = TicketStore(session_factory)
    pending = _pending()
    created = asyncio.run(repo.create(_ticket(ticket_no="T-P3-0003", pending=pending)))
    asyncio.run(
        repo.transition(
            tenant_id=TENANT,
            ticket_id=created.ticket_id,
            target=TicketState.PENDING_APPROVAL,
            trigger="preview_ok",
            context=TransitionContext(preview_ok=True),
        )
    )

    online = asyncio.run(repo.get(tenant_id=TENANT, ticket_id=created.ticket_id))
    rebuilt = asyncio.run(store.rebuild_projection(created.ticket_id))
    assert online is not None
    # 逐字段比对：API 写的这几个字段必须能从事件重建出来。
    assert rebuilt.org_id == online.org_id == ORG
    assert rebuilt.period == online.period == PERIOD
    assert rebuilt.systems == online.systems == (SYSTEM,)
    assert rebuilt.write_level == int(online.write_level) == int(WriteLevel.W1)
    assert rebuilt.amount == online.amount == Decimal("1200.00")
    assert rebuilt.pending_write == {
        "action_key": pending.action_key,
        "system": pending.system,
        "payload": dict(pending.payload),
        "idem_key": pending.idem_key,
    }
    assert rebuilt.state == online.state == TicketState.PENDING_APPROVAL
    assert rebuilt.version + 1 == online.version

    # 驳回会清空写计划：事件里显式写 pending_write: null，重建后必须也是空。
    asyncio.run(
        repo.transition(
            tenant_id=TENANT,
            ticket_id=created.ticket_id,
            target=TicketState.REJECTED,
            trigger="rejected",
            context=TransitionContext(),
            payload={"pending_write": None},
        )
    )
    online = asyncio.run(repo.get(tenant_id=TENANT, ticket_id=created.ticket_id))
    rebuilt = asyncio.run(store.rebuild_projection(created.ticket_id))
    assert online is not None
    assert rebuilt.pending_write is None
    assert online.pending_write is None
    assert rebuilt.state == online.state == TicketState.REJECTED
    assert rebuilt.version + 1 == online.version
    assert asyncio.run(store.check_projection(created.ticket_id)).consistent
