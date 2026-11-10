"""S3 测试夹具：临时 PG15（``ERP_TEST_DATABASE_URL``）。

- 先 ``alembic upgrade head`` 建 S0 基线四表；
- 再按 ``apps/agents/MIGRATION_NEEDS.md`` 建 ``compensation_task`` 与两条结构约束
  （S0 合并迁移前，测试自己补，保证运行时代码可跑）；
- 没设置环境变量时整组 DB 用例 skip，纯逻辑用例照常运行。
"""

from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from apps.agents.models import ADDITIONAL_DDL, CompensationTask


async def _wait_ready(url: str, attempts: int = 60) -> None:
    last: Exception | None = None
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        for _ in range(attempts):
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                await asyncio.sleep(1)
    finally:
        await engine.dispose()
    raise RuntimeError(f"测试库不可用: {last}")


def _upgrade(url: str) -> None:
    config = Config(str(os.path.join(os.getcwd(), "alembic.ini")))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", os.path.join(os.getcwd(), "db", "migrations"))
    command.upgrade(config, "head")


async def _create_compensation_table(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(CompensationTask.__table__.create, checkfirst=True)
            for ddl in ADDITIONAL_DDL:
                await conn.execute(text(ddl))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("ERP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("未设置 ERP_TEST_DATABASE_URL，跳过 PG 集成用例")
    asyncio.run(_wait_ready(url))
    _upgrade(url)
    asyncio.run(_create_compensation_table(url))
    return url


async def _truncate(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE erp.ticket_event, erp.ticket_step, "
                    "erp.compensation_task, erp.ticket_event_anchor, "
                    "erp.audit_guard_state, erp.job_queue, "
                    "erp.worker_heartbeat, erp.scheduler_lease, "
                    "erp.ticket CASCADE"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture()
def db(pg_url: str) -> str:
    asyncio.run(_truncate(pg_url))
    return pg_url
