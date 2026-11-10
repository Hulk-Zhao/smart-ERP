"""DB 测试脚手架：每个用例一个事件循环 + NullPool 引擎（避免跨 loop 复用连接）。"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from apps.agents.compensation import CompensationQueue
from apps.agents.lease import LeaseManager
from apps.agents.state_machine import TransitionContext
from apps.agents.store import TicketStore
from apps.platform.db.models import Ticket

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
USER_ID = UUID("22222222-2222-2222-2222-222222222222")
OTHER_TENANT_ID = UUID("33333333-3333-3333-3333-333333333333")


@dataclass
class DbEnv:
    url: str
    engine: AsyncEngine
    sf: async_sessionmaker[AsyncSession]
    store: TicketStore
    leases: LeaseManager
    queue: CompensationQueue


@asynccontextmanager
async def db_env(url: str) -> AsyncIterator[DbEnv]:
    engine = create_async_engine(url, poolclass=NullPool)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield DbEnv(
            url=url,
            engine=engine,
            sf=sf,
            store=TicketStore(sf),
            leases=LeaseManager(sf),
            queue=CompensationQueue(sf),
        )
    finally:
        await engine.dispose()


async def truncate_all(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE erp.ticket_event, erp.ticket_step, erp.compensation_task, "
                    "erp.ticket_event_anchor, erp.audit_guard_state, erp.job_queue, "
                    "erp.worker_heartbeat, erp.scheduler_lease, erp.ticket CASCADE"
                )
            )
    finally:
        await engine.dispose()


async def new_ticket(
    store: TicketStore,
    *,
    scene_code: str = "AG-1",
    business_key: str | None = None,
    diff_amount: Decimal | None = None,
    tenant_id: UUID = TENANT_ID,
) -> Ticket:
    key = business_key or f"bk-{uuid4().hex[:12]}"
    return await store.create_ticket(
        tenant_id=tenant_id,
        ticket_no=f"AT-20261110-{random.randint(0, 9999):04d}",
        scene_code=scene_code,
        initiator_id=USER_ID,
        trigger_kind="timer",
        business_key=key,
        actor_id=USER_ID,
        trace_id=f"trace-{uuid4().hex[:8]}",
        diff_amount=diff_amount,
        systems_count=2,
    )


async def prepare_executing(
    store: TicketStore, *, scene_code: str = "AG-1"
) -> Ticket:
    """建单 → 预演通过 → 审批通过，返回「执行中」的工单。"""

    ticket = await new_ticket(store, scene_code=scene_code)
    await store.transition(
        ticket.id,
        "待审批",
        "preview_ok",
        context=TransitionContext(preview_ok=True),
    )
    await store.transition(
        ticket.id,
        "执行中",
        "approved",
        context=TransitionContext(approved=True),
    )
    return ticket
