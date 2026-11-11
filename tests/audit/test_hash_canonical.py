"""审计哈希的时间规范形式：同一时刻不能因 tzinfo 不同而算出不同哈希。

回归的是生产缺陷：``created_at`` 用 ``str(datetime)`` 序列化时，带 ``+08:00`` 的
写入值与从 PG 读回的 UTC ``timestamptz`` 得到不同字符串，真实数据会被验成
「疑似篡改」。规范形式见 ``apps/platform/contracts/audit.py`` 的冻结口径。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.audit.store import SqlAuditStore
from apps.platform.contracts.audit import (
    AuditImage,
    canonical_json,
    compute_image_hash,
    verify_chain,
)

CST = timezone(timedelta(hours=8))
WRITTEN = datetime(2026, 8, 16, 15, 21, 28, 123456, tzinfo=CST)
SAME_INSTANT_UTC = datetime(2026, 8, 16, 7, 21, 28, 123456, tzinfo=UTC)

_DSN = os.environ.get("ERP_TEST_DATABASE_URL")


def test_canonical_json_normalizes_aware_datetime_to_utc() -> None:
    assert canonical_json({"created_at": WRITTEN}) == canonical_json(
        {"created_at": SAME_INSTANT_UTC}
    )
    assert (
        canonical_json({"created_at": WRITTEN})
        == '{"created_at":"2026-08-16T07:21:28.123456+00:00"}'
    )


def test_canonical_json_treats_naive_datetime_as_utc() -> None:
    naive = datetime(2026, 8, 16, 7, 21, 28, 123456)
    assert canonical_json({"created_at": naive}) == canonical_json(
        {"created_at": SAME_INSTANT_UTC}
    )


def test_nested_field_diff_timestamp_uses_same_rule() -> None:
    assert canonical_json(
        {"field_diff": [{"before": WRITTEN, "after": None}]}
    ) == canonical_json({"field_diff": [{"before": SAME_INSTANT_UTC, "after": None}]})


def _dsn() -> str:
    return (
        (_DSN or "")
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg://", "postgresql://")
    )


def _upgrade() -> None:
    config = Config(str(os.path.join(os.getcwd(), "alembic.ini")))
    config.set_main_option("sqlalchemy.url", _DSN or "")
    config.set_main_option(
        "script_location", os.path.join(os.getcwd(), "db", "migrations")
    )
    command.upgrade(config, "head")


def _seed_ticket() -> tuple[UUID, UUID]:
    async def main() -> tuple[UUID, UUID]:
        conn = await asyncpg.connect(_dsn())
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
                f"HT-{ticket_id.hex[:8]}",
                uuid4(),
                f"bk-{ticket_id.hex[:8]}",
            )
            return tenant_id, ticket_id
        finally:
            await conn.close()

    _upgrade()
    return asyncio.run(main())


@pytest.mark.skipif(not _DSN, reason="未设置 ERP_TEST_DATABASE_URL，跳过 PG 往返用例")
def test_plus8_created_at_survives_pg_roundtrip() -> None:
    """写入用 +08:00，读回是 UTC：重算 hash 必须与写入时一致且整链校验通过。"""

    tenant_id, ticket_id = _seed_ticket()

    async def main() -> None:
        engine = create_async_engine(_DSN or "")
        store = SqlAuditStore(async_sessionmaker(engine, expire_on_commit=False))
        try:
            image = AuditImage(
                id=uuid4(),
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                step_id=uuid4(),
                system="fake",
                object_type="invoice_draft",
                object_id="OBJ-TZ",
                field_diff=[],
                prev_hash="",
                hash="0" * 64,
                created_at=WRITTEN,
            )
            written_hash = compute_image_hash("", image.model_dump(mode="python"))
            await store.append(image.model_copy(update={"hash": written_hash}))

            chain = await store.chain(tenant_id)
            assert len(chain) == 1
            assert chain[0].hash == written_hash
            assert (
                compute_image_hash("", chain[0].model_dump(mode="python"))
                == written_hash
            ), "从 PG 读回后重算的 hash 与写入时不一致"
            assert verify_chain(chain) is True
        finally:
            await engine.dispose()

    asyncio.run(main())
