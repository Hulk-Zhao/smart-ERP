"""``SqlTicketRepository`` 与内存实现的读路径一致性（PR2）。

种子特意覆盖边界值：``systems``/``amount`` 为 NULL、``pending_write`` 为空对象、
``version = 0`` 的历史行；再加三条**同一 ``created_at``、不同 ``id``** 的行，用来
证明键集分页在这个最容易丢行的场景上不重不漏（丢行是静默的，不报错）。

跳过策略与 ``test_recon_store_sql.py`` 一致：未设置 ``ERP_TEST_DATABASE_URL`` 时
整组 skip 并写明原因；CI 的 PG15 service 会设置该变量，故 CI 上必须真跑。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.api.store import (
    PendingWrite,
    TicketRecord,
    TicketRepository,
    TicketStepRecord,
)
from apps.api.ticket_sql import SqlTicketRepository
from apps.platform.contracts.ticket import TicketState
from apps.platform.db.models import Ticket, TicketStep
from apps.platform.policy.levels import WriteLevel
from apps.security.scope import Scope

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TENANT = UUID("11111111-1111-1111-1111-111111111111")
INITIATOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG = "ORG-1"
PERIOD = "2026-10"
SCENE = "ag2_processing_fee"
SYSTEM = "u8"

SCOPE = Scope(org_id=ORG, period=PERIOD, system_ids=(SYSTEM, "u9"))


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


def _row(
    *,
    seq: int,
    created_at: datetime,
    systems: list[str] | None,
    version: int = 1,
    amount: str | None = "1200.00",
    write_level: int | None = 1,
    state: str = TicketState.PENDING_APPROVAL.value,
    pending_write: dict | None = None,
) -> dict:
    return {
        "id": UUID(int=seq),
        "tenant_id": TENANT,
        "ticket_no": f"T-{seq:04d}",
        "scene_code": SCENE,
        "initiator_id": INITIATOR,
        "trigger_kind": "manual",
        "business_key": f"BK-{seq}",
        "org_id": ORG,
        "period": PERIOD,
        "systems": systems,
        "state": state,
        "version": version,
        "write_level": write_level,
        "amount": Decimal(amount) if amount is not None else None,
        "diff_amount": None,
        "systems_count": len(systems) if systems is not None else None,
        "touches_fund": False,
        "pending_write": pending_write,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _step(*, seq: int, step_id: int, step_no: int, level: int = 1) -> dict:
    at = datetime(2026, 11, 10, 9, 5, 0, tzinfo=UTC)
    return {
        "id": UUID(int=step_id),
        "ticket_id": UUID(int=seq),
        "step_no": step_no,
        "action_key": "u8.trade.save_draft",
        "target_system": SYSTEM,
        "write_level": level,
        "state": "succeeded",
        "idempotency_key": f"idem-{seq}-{step_no}",
        "request_hash": f"hash-{seq}-{step_no}",
        "attempt": 1,
        "executed_at": at,
        "object_type": "draft",
        "object_id": f"D-{seq}-{step_no}",
        "audit_image_id": UUID(int=9000 + step_id),
    }


async def _seed(session_factory, *, tickets: list[dict], steps: list[dict]) -> None:
    async with session_factory() as session:
        await session.execute(sa.insert(Ticket.__table__), tickets)
        if steps:
            await session.execute(sa.insert(TicketStep.__table__), steps)
        await session.commit()


def _record(
    row: dict,
    *,
    systems: tuple[str, ...] | None,
    amount: Decimal | None,
    write_level: WriteLevel | None,
    pending_write: PendingWrite | None,
    steps: list[TicketStepRecord] | None = None,
) -> TicketRecord:
    return TicketRecord(
        ticket_id=row["id"],
        tenant_id=TENANT,
        ticket_no=row["ticket_no"],
        scene_code=SCENE,
        initiator_id=INITIATOR,
        business_key=row["business_key"],
        org_id=ORG,
        period=PERIOD,
        systems=systems,
        write_level=write_level,
        amount=amount,
        touches_fund=False,
        state=TicketState(row["state"]),
        # DB 存迁移次数（建单 0），端口对外 +1（建单即 1）。
        version=row["version"] + 1,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        pending_write=pending_write,
        steps=steps or [],
    )


def _fields(ticket: TicketRecord) -> tuple:
    return (
        ticket.ticket_id,
        ticket.tenant_id,
        ticket.ticket_no,
        ticket.scene_code,
        ticket.initiator_id,
        ticket.business_key,
        ticket.org_id,
        ticket.period,
        ticket.systems,
        ticket.write_level,
        ticket.amount,
        ticket.touches_fund,
        ticket.state,
        ticket.version,
        ticket.created_at,
        ticket.updated_at,
        ticket.pending_write,
    )


def _step_fields(step: TicketStepRecord) -> tuple:
    return (
        step.step_id,
        step.step_no,
        step.action_key,
        step.system,
        step.object_type,
        step.object_id,
        step.level,
        step.idem_key,
        step.state,
        step.executed_at,
        step.audit_image_id,
    )


def _seed_rows() -> tuple[list[dict], list[dict]]:
    same_second = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
    tickets = [
        _row(
            seq=1,
            created_at=same_second,
            systems=[SYSTEM],
            version=2,
            pending_write={
                "action_key": "u8.trade.save_draft",
                "system": SYSTEM,
                "payload": {"draft_id": "D-1"},
                "idem_key": "idem-1",
            },
        ),
        _row(seq=2, created_at=same_second, systems=[SYSTEM], amount=None),
        _row(seq=3, created_at=same_second, systems=[SYSTEM]),
        # 执行器历史行：systems/amount/write_level 都未知，pending_write 是空对象。
        _row(
            seq=4,
            created_at=datetime(2026, 11, 10, 8, 0, 0, tzinfo=UTC),
            systems=None,
            version=0,
            amount=None,
            write_level=None,
            state=TicketState.PLANNED.value,
            pending_write={},
        ),
    ]
    steps = [
        _step(seq=1, step_id=101, step_no=1),
        _step(seq=1, step_id=102, step_no=2),
    ]
    return tickets, steps


def test_list_matches_in_memory_including_boundary_rows(session_factory):
    tickets, steps = _seed_rows()
    asyncio.run(_seed(session_factory, tickets=tickets, steps=steps))

    sql_repo = SqlTicketRepository(session_factory=session_factory)
    memory_repo = TicketRepository(
        [
            _record(
                tickets[0],
                systems=(SYSTEM,),
                amount=Decimal("1200.00"),
                write_level=WriteLevel.W1,
                pending_write=PendingWrite(
                    action_key="u8.trade.save_draft",
                    system=SYSTEM,
                    payload={"draft_id": "D-1"},
                    idem_key="idem-1",
                ),
                steps=[
                    TicketStepRecord(
                        step_id=UUID(int=101),
                        step_no=1,
                        action_key="u8.trade.save_draft",
                        system=SYSTEM,
                        object_type="draft",
                        object_id="D-1-1",
                        level=WriteLevel.W1,
                        idem_key="idem-1-1",
                        executed_at=datetime(2026, 11, 10, 9, 5, 0, tzinfo=UTC),
                        audit_image_id=UUID(int=9101),
                    ),
                    TicketStepRecord(
                        step_id=UUID(int=102),
                        step_no=2,
                        action_key="u8.trade.save_draft",
                        system=SYSTEM,
                        object_type="draft",
                        object_id="D-1-2",
                        level=WriteLevel.W1,
                        idem_key="idem-1-2",
                        executed_at=datetime(2026, 11, 10, 9, 5, 0, tzinfo=UTC),
                        audit_image_id=UUID(int=9102),
                    ),
                ],
            ),
            _record(
                tickets[1],
                systems=(SYSTEM,),
                amount=None,
                write_level=WriteLevel.W1,
                pending_write=None,
            ),
            _record(
                tickets[2],
                systems=(SYSTEM,),
                amount=Decimal("1200.00"),
                write_level=WriteLevel.W1,
                pending_write=None,
            ),
            _record(
                tickets[3],
                systems=None,
                amount=None,
                write_level=None,
                pending_write=None,
            ),
        ]
    )

    sql_page, sql_cursor = asyncio.run(
        sql_repo.list(tenant_id=TENANT, scope=SCOPE, limit=20)
    )
    memory_page, memory_cursor = asyncio.run(
        memory_repo.list(tenant_id=TENANT, scope=SCOPE, limit=20)
    )

    assert [_fields(row) for row in sql_page] == [_fields(row) for row in memory_page]
    assert sql_cursor == memory_cursor
    # NULL systems 的历史行不可见，但它必须能被 get 映射出来（见下一个用例）。
    assert [row.ticket_no for row in sql_page] == ["T-0003", "T-0002", "T-0001"]


def test_get_matches_in_memory_including_steps(session_factory):
    tickets, steps = _seed_rows()
    asyncio.run(_seed(session_factory, tickets=tickets, steps=steps))
    sql_repo = SqlTicketRepository(session_factory=session_factory)

    sql_ticket = asyncio.run(sql_repo.get(tenant_id=TENANT, ticket_id=tickets[0]["id"]))
    assert sql_ticket is not None
    assert [row.ticket_no for row in [sql_ticket]] == ["T-0001"]
    assert sql_ticket.pending_write == PendingWrite(
        action_key="u8.trade.save_draft",
        system=SYSTEM,
        payload={"draft_id": "D-1"},
        idem_key="idem-1",
    )
    assert [_step_fields(step) for step in sql_ticket.steps] == [
        (
            UUID(int=101),
            1,
            "u8.trade.save_draft",
            SYSTEM,
            "draft",
            "D-1-1",
            WriteLevel.W1,
            "idem-1-1",
            "succeeded",
            datetime(2026, 11, 10, 9, 5, 0, tzinfo=UTC),
            UUID(int=9101),
        ),
        (
            UUID(int=102),
            2,
            "u8.trade.save_draft",
            SYSTEM,
            "draft",
            "D-1-2",
            WriteLevel.W1,
            "idem-1-2",
            "succeeded",
            datetime(2026, 11, 10, 9, 5, 0, tzinfo=UTC),
            UUID(int=9102),
        ),
    ]

    historical = asyncio.run(sql_repo.get(tenant_id=TENANT, ticket_id=tickets[3]["id"]))
    assert historical is not None
    assert historical.systems is None
    assert historical.amount is None
    assert historical.write_level is None
    # DB 里是 0（从未迁移），对外必须是 1：前端乐观并发的起点不能变成 0。
    assert historical.version == 1
    # 空对象与 NULL 同义：没有锁定写计划。
    assert historical.pending_write is None
    assert historical.steps == []
    # 跨租户取不到。
    assert (
        asyncio.run(sql_repo.get(tenant_id=UUID(int=777), ticket_id=tickets[0]["id"]))
        is None
    )


def test_keyset_pagination_on_equal_created_at_loses_no_rows(session_factory):
    same_second = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
    tickets = [
        _row(seq=seq, created_at=same_second, systems=[SYSTEM]) for seq in (1, 2, 3)
    ]
    asyncio.run(_seed(session_factory, tickets=tickets, steps=[]))

    sql_repo = SqlTicketRepository(session_factory=session_factory)
    memory_repo = TicketRepository(
        [
            _record(
                row,
                systems=(SYSTEM,),
                amount=Decimal("1200.00"),
                write_level=WriteLevel.W1,
                pending_write=None,
            )
            for row in tickets
        ]
    )

    sql_first, sql_cursor = asyncio.run(
        sql_repo.list(tenant_id=TENANT, scope=SCOPE, limit=2)
    )
    sql_second, sql_tail = asyncio.run(
        sql_repo.list(tenant_id=TENANT, scope=SCOPE, limit=2, cursor=sql_cursor)
    )
    memory_first, memory_cursor = asyncio.run(
        memory_repo.list(tenant_id=TENANT, scope=SCOPE, limit=2)
    )
    memory_second, memory_tail = asyncio.run(
        memory_repo.list(tenant_id=TENANT, scope=SCOPE, limit=2, cursor=memory_cursor)
    )

    sql_ids = [row.ticket_id for row in sql_first + sql_second]
    memory_ids = [row.ticket_id for row in memory_first + memory_second]
    # 同一秒内按 id 倒序，跨页不重不漏。
    assert sql_ids == [UUID(int=3), UUID(int=2), UUID(int=1)]
    assert sql_ids == memory_ids
    assert sql_cursor == memory_cursor
    assert sql_tail is None and memory_tail is None
