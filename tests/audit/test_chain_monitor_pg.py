"""分页巡检在真实 PG 上的行为：游标翻页、跨页链尾衔接、篡改检测。

单测里 ``chain_page`` 走的是内存实现；这里补 SQL 路径，避免「内存绿、SQL 红」。
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.audit.ledger import AuditLedger
from apps.audit.monitor import AuditChainMonitor
from apps.audit.store import SqlAuditStore
from observability.metrics import MetricsRegistry

_DSN = os.environ.get("ERP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="未设置 ERP_TEST_DATABASE_URL，跳过 PG 分页巡检用例"
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


def test_pg_paging_detects_tamper_across_pages() -> None:
    _upgrade()
    tenant_id, ticket_id = _seed_ticket()

    async def main() -> None:
        engine = create_async_engine(_DSN or "")
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            ledger = AuditLedger(store=SqlAuditStore(factory))
            for hour in range(10, 13):
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
            metrics = MetricsRegistry()
            # 每页 1 行、每片 2 行：一条 3 行的链必然跨两个时间片、三个页。
            monitor = AuditChainMonitor(
                ledger=ledger,
                sink=metrics,
                rows_per_run=2,
                page_size=1,
                interval_seconds=0.0,
                clock=lambda: 0.0,
                # 共享测试库里还有别的用例留下的租户，这里钉死只扫本用例的链。
                tenants=[tenant_id],
            )

            first = await monitor.step()
            assert first is not None and first.cycle_completed is False
            assert first.rows == 2
            second = await monitor.step()
            assert second is not None and second.cycle_completed is True
            assert metrics.audit_chain_broken.value() == 0.0

            async with factory() as session, session.begin():
                from sqlalchemy import text

                # 生产库里 UPDATE 被 append-only 触发器拒绝，真正的篡改只可能来自
                # 有 DBA 权限的人先关触发器——这里就这么造，顺便证明触发器确实在拦。
                await session.execute(text("ALTER TABLE erp.audit_image DISABLE TRIGGER USER"))
                await session.execute(
                    text(
                        "UPDATE erp.audit_image SET hash = repeat('0', 64) "
                        "WHERE tenant_id = :t AND chain_seq = ("
                        "  SELECT min(chain_seq) FROM erp.audit_image WHERE tenant_id = :t"
                        ")"
                    ),
                    {"t": tenant_id},
                )
                await session.execute(text("ALTER TABLE erp.audit_image ENABLE TRIGGER USER"))

            third = await monitor.step()
            assert third is not None and third.broken_at == 0
            assert metrics.audit_chain_broken.value() == 1.0
        finally:
            await engine.dispose()

    asyncio.run(main())
