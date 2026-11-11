"""日切锚点在真实 PG 上的行为：调度写入、库内真实行、追加后仍判对、并发不覆盖。

内存用例见 ``test_anchor_service.py``；这里补 SQL 路径与调度接线，避免「内存绿、SQL 红」。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.audit.anchor import AuditAnchorService
from apps.audit.ledger import AuditLedger
from apps.audit.models import AuditAnchorRow
from apps.audit.store import AuditAnchor, SqlAuditStore
from core.runtime.scheduler import Scheduler

_DSN = os.environ.get("ERP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="未设置 ERP_TEST_DATABASE_URL，跳过 PG 锚点用例"
)


def _raw_dsn() -> str:
    return (
        (_DSN or "")
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg://", "postgresql://")
    )


def _upgrade() -> None:
    config = Config(os.path.join(os.getcwd(), "alembic.ini"))
    config.set_main_option("sqlalchemy.url", _DSN or "")
    config.set_main_option("script_location", os.path.join(os.getcwd(), "db", "migrations"))
    command.upgrade(config, "head")


def _seed_ticket() -> tuple[UUID, UUID]:
    async def main() -> tuple[UUID, UUID]:
        conn = await asyncpg.connect(_raw_dsn())
        try:
            tenant_id, ticket_id = uuid4(), uuid4()
            await conn.execute(
                """
                INSERT INTO erp.ticket
                    (id, tenant_id, ticket_no, scene_code, initiator_id,
                     trigger_kind, business_key, state)
                VALUES ($1, $2, $3, 'AG-1', $4, 'timer', $5, '计划中')
                """,
                ticket_id,
                tenant_id,
                f"AC-{ticket_id.hex[:8]}",
                uuid4(),
                f"bk-{ticket_id.hex[:8]}",
            )
            return tenant_id, ticket_id
        finally:
            await conn.close()

    return asyncio.run(main())


async def _anchor_rows(tenant_id: UUID) -> list[asyncpg.Record]:
    """绕过 ORM 直接读库：库内真实行数与内容才是证据。"""

    conn = await asyncpg.connect(_raw_dsn())
    try:
        return await conn.fetch(
            "SELECT tenant_id, anchor_date, record_count, last_hash, created_at "
            "FROM erp.audit_anchor WHERE tenant_id = $1 ORDER BY anchor_date",
            tenant_id,
        )
    finally:
        await conn.close()


def test_scheduler_tick_writes_real_anchor_and_survives_append() -> None:
    _upgrade()
    tenant_id, ticket_id = _seed_ticket()
    day = datetime.now(UTC).date()

    async def main() -> None:
        engine = create_async_engine(_DSN or "")
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            ledger = AuditLedger(store=SqlAuditStore(factory))
            for hour in (10, 11):
                await ledger.append_image(
                    tenant_id=tenant_id,
                    ticket_id=ticket_id,
                    step_id=uuid4(),
                    system="fake",
                    object_type="invoice_draft",
                    object_id=f"D-{hour}",
                    before={"amount": "100.00"},
                    after={"amount": "120.00"},
                )
            tail = await ledger.store.last_hash(tenant_id)

            scheduler = Scheduler(
                session_factory=factory,
                owner=f"test:{uuid4().hex[:8]}",
                audit_image_scan=False,
                audit_anchor=True,
            )
            assert scheduler.anchor_service is not None
            # 共享测试库里还有别的租户，钉死只锚本用例租户（生产按 audit_image 自动发现）
            scheduler.anchor_service.tenants = [tenant_id]

            await scheduler.tick()
            rows = await _anchor_rows(tenant_id)
            assert len(rows) == 1
            row = rows[0]
            assert row["anchor_date"] == day
            assert row["record_count"] == 2
            assert row["last_hash"] == tail
            assert scheduler.stats.anchor_writes == 1

            # 锚点之后继续追加：链完整性校验必须判对（B-ANCHOR 的核心缺陷场景）
            for hour in (12, 13):
                await ledger.append_image(
                    tenant_id=tenant_id,
                    ticket_id=ticket_id,
                    step_id=uuid4(),
                    system="fake",
                    object_type="invoice_draft",
                    object_id=f"D-{hour}",
                    before={"amount": "100.00"},
                    after={"amount": "120.00"},
                )
            anchor = await ledger.store.get_anchor(tenant_id, day)
            assert anchor is not None
            assert (await ledger.verify_against_anchor(anchor)).ok is True
            assert (await ledger.verify(tenant_id)).ok is True

            # 同一天再跑：不得重写已存锚点（写重了比没写更糟）
            await scheduler.tick()
            again = await _anchor_rows(tenant_id)
            assert len(again) == 1
            assert dict(again[0]) == dict(row)
            assert (await ledger.anchor_day(tenant_id, day)) == anchor
        finally:
            await engine.dispose()

    asyncio.run(main())


def test_concurrent_anchor_writes_keep_exactly_one_row() -> None:
    _upgrade()
    tenant_id, ticket_id = _seed_ticket()
    day = datetime.now(UTC).date()

    async def main() -> None:
        engine = create_async_engine(_DSN or "")
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            ledger = AuditLedger(store=SqlAuditStore(factory))
            await ledger.append_image(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                step_id=uuid4(),
                system="fake",
                object_type="invoice_draft",
                object_id="D-1",
                before={"amount": "100.00"},
                after={"amount": "120.00"},
            )
            # 两个「副本」同时写同一天：主键只允许一行，后提交者重读赢家而不是覆盖
            left = AuditLedger(store=SqlAuditStore(factory))
            right = AuditLedger(store=SqlAuditStore(factory))
            first, second = await asyncio.gather(
                left.anchor_day(tenant_id, day), right.anchor_day(tenant_id, day)
            )
            assert first == second

            rows = await _anchor_rows(tenant_id)
            assert len(rows) == 1
            assert rows[0]["record_count"] == 1
            assert rows[0]["last_hash"] == first.last_hash
            assert (await left.verify_against_anchor(first)).ok is True

            async with factory() as session, session.begin():
                # 只读核对：确认写入的锚点没有被 UPDATE 过（append-only 触发器之外的口径）
                stored = (
                    await session.execute(
                        text(
                            "SELECT record_count, last_hash FROM erp.audit_anchor "
                            "WHERE tenant_id = :t AND anchor_date = :d"
                        ),
                        {"t": tenant_id, "d": day},
                    )
                ).one()
            assert tuple(stored) == (1, first.last_hash)
        finally:
            await engine.dispose()

    asyncio.run(main())


class _OtherReplicaFirstStore(SqlAuditStore):
    """模拟另一副本抢先写入：在本次 INSERT 之前先落一行同主键的锚点。"""

    def __init__(self, session_factory, winner: AuditAnchor) -> None:
        super().__init__(session_factory)
        self._winner = winner

    async def record_anchor(self, anchor: AuditAnchor) -> None:
        row = AuditAnchorRow(
            tenant_id=self._winner.tenant_id,
            anchor_date=self._winner.anchor_date,
            last_hash=self._winner.last_hash,
            record_count=self._winner.record_count,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
        await super().record_anchor(anchor)  # 必然撞主键


def test_pg_conflict_keeps_winner_anchor_even_if_shorter() -> None:
    """后提交者撞主键后只能沿用赢家锚点：即使赢家的前缀更短也不能覆盖。"""

    _upgrade()
    tenant_id, ticket_id = _seed_ticket()
    day = datetime.now(UTC).date()

    async def main() -> None:
        engine = create_async_engine(_DSN or "")
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            ledger = AuditLedger(store=SqlAuditStore(factory))
            first = await ledger.append_image(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                step_id=uuid4(),
                system="fake",
                object_type="invoice_draft",
                object_id="D-1",
                before={"amount": "100.00"},
                after={"amount": "120.00"},
            )
            await ledger.append_image(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                step_id=uuid4(),
                system="fake",
                object_type="invoice_draft",
                object_id="D-2",
                before={"amount": "100.00"},
                after={"amount": "120.00"},
            )
            # 赢家只看到 1 条；本次实现读到 2 条，但撞主键后必须沿用赢家的 1 条
            winner = AuditAnchor(tenant_id, day, first.hash, 1)
            racing = AuditLedger(store=_OtherReplicaFirstStore(factory, winner))

            result = await racing.anchor_day(tenant_id, day)
            assert result == winner
            rows = await _anchor_rows(tenant_id)
            assert len(rows) == 1
            assert rows[0]["record_count"] == 1
            assert rows[0]["last_hash"] == first.hash
            assert (await racing.verify_against_anchor(winner)).ok is True
        finally:
            await engine.dispose()

    asyncio.run(main())


@dataclass
class _FakeClock:
    now_value: datetime
    monotonic_value: float = 1000.0

    def now(self) -> datetime:
        return self.now_value

    def monotonic(self) -> float:
        return self.monotonic_value


def test_pg_scheduler_tick_survives_cross_day_rollback() -> None:
    """端到端：进程时钟跨日回拨时 tick 不报错、不补写过去日、不重算前缀。"""

    _upgrade()
    tenant_id, ticket_id = _seed_ticket()

    async def main() -> None:
        engine = create_async_engine(_DSN or "")
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            ledger = AuditLedger(store=SqlAuditStore(factory))
            clock = _FakeClock(datetime(2026, 11, 10, 23, 59, 30, tzinfo=UTC))
            scheduler = Scheduler(
                session_factory=factory,
                owner=f"test:{uuid4().hex[:8]}",
                audit_image_scan=False,
                audit_anchor=True,
            )
            scheduler.anchor_service = AuditAnchorService(
                ledger=AuditLedger(store=SqlAuditStore(factory)),
                clock=clock.now,
                monotonic=clock.monotonic,
                tenants=[tenant_id],
                retry_seconds=0.0,
            )

            async def append(object_id: str, stamp: datetime) -> None:
                await ledger.append_image(
                    tenant_id=tenant_id,
                    ticket_id=ticket_id,
                    step_id=uuid4(),
                    system="fake",
                    object_type="invoice_draft",
                    object_id=object_id,
                    before={"amount": "100.00"},
                    after={"amount": "120.00"},
                    created_at=stamp,
                )

            await append("D-1", datetime(2026, 11, 10, 23, 59, 30, tzinfo=UTC))
            await scheduler.tick()  # 锚 11-10

            clock.now_value = datetime(2026, 11, 11, 0, 0, 10, tzinfo=UTC)
            clock.monotonic_value += 5.0
            await append("D-2", datetime(2026, 11, 11, 0, 0, 10, tzinfo=UTC))
            await scheduler.tick()  # 锚 11-11

            # 时钟回拨到昨天 23:59:50，这条晚写的行盖的是旧日期
            clock.now_value = datetime(2026, 11, 10, 23, 59, 50, tzinfo=UTC)
            clock.monotonic_value += 5.0
            await append("D-3", datetime(2026, 11, 10, 23, 59, 50, tzinfo=UTC))
            result = await scheduler.tick()

            assert "audit_anchor_error" not in result
            assert scheduler.stats.anchor_errors == 0
            rows = await _anchor_rows(tenant_id)
            assert [row["anchor_date"] for row in rows] == [
                date(2026, 11, 10),
                date(2026, 11, 11),
            ]
            assert [row["record_count"] for row in rows] == [1, 2]

            first = await ledger.store.get_anchor(tenant_id, date(2026, 11, 10))
            second = await ledger.store.get_anchor(tenant_id, date(2026, 11, 11))
            assert (await ledger.verify_against_anchor(first)).ok is True
            assert (await ledger.verify_against_anchor(second)).ok is True
            assert (await ledger.verify(tenant_id)).ok is True
        finally:
            await engine.dispose()

    asyncio.run(main())
