"""``audit_image`` 同父分叉：唯一约束把并发追加变成「一条失败 + 重试成功」。

两个会话读到同一个链尾后同时追加，是审计链最危险的静默污染：此前两行都会通过
``fn_audit_image_chain_guard``（快照看不到对方未提交的行），链就分叉成两截。
``uq_audit_image_parent (tenant_id, prev_hash)`` 把这种情况变成库层拒绝。

这里用 ``REPEATABLE READ`` 冻结第二个会话的快照，精确复现「两个写入者看到同一个
链尾」，不依赖 sleep 抢时序。
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

_DSN = os.environ.get("ERP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="未设置 ERP_TEST_DATABASE_URL，跳过 PG 并发用例"
)

HASH_A = "a" * 64
HASH_B = "b" * 64

_INSERT = """
INSERT INTO erp.audit_image
    (id, tenant_id, ticket_id, step_id, system, object_type, object_id,
     field_diff, diff_missing, prev_hash, hash)
VALUES ($1, $2, $3, $4, 'fake', 'invoice_draft', $5, '[]'::jsonb, false, $6, $7)
"""

_TAIL = """
SELECT hash FROM erp.audit_image
 WHERE tenant_id = $1
 ORDER BY chain_seq DESC
 LIMIT 1
"""


def _dsn() -> str:
    raw = _DSN or ""
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _upgrade(url: str) -> None:
    config = Config(str(os.path.join(os.getcwd(), "alembic.ini")))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option(
        "script_location", os.path.join(os.getcwd(), "db", "migrations")
    )
    command.upgrade(config, "head")


def _seed_ticket() -> tuple[UUID, UUID]:
    """跑迁移并插入一张工单（复合外键要求 tenant_id 与工单一致）。"""

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
                f"AT-{ticket_id.hex[:8]}",
                uuid4(),
                f"bk-{ticket_id.hex[:8]}",
            )
            return tenant_id, ticket_id
        finally:
            await conn.close()

    _upgrade(_DSN or "")
    return asyncio.run(main())


def test_concurrent_append_on_same_tail_yields_single_integrity_error() -> None:
    tenant_id, ticket_id = _seed_ticket()

    async def main() -> None:
        reader = await asyncpg.connect(_dsn())
        writer = await asyncpg.connect(_dsn())
        try:
            # 1) 读会话先固定快照：此时链尾为空
            await reader.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            tail = await reader.fetchval(_TAIL, tenant_id)
            assert tail is None, "用例要求空链，便于固定 prev_hash=''"

            # 2) 另一个会话完成创世行并提交
            await writer.execute(
                _INSERT, uuid4(), tenant_id, ticket_id, uuid4(), "OBJ-A", "", HASH_A
            )

            # 3) 读会话携旧快照插入：链尾断言通过，唯一约束拒绝
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await reader.execute(
                    _INSERT, uuid4(), tenant_id, ticket_id, uuid4(), "OBJ-B", "", HASH_B
                )
            await reader.execute("ROLLBACK")

            # 4) 重试：重读链尾后成功，链保持单线
            new_tail = await reader.fetchval(_TAIL, tenant_id)
            assert new_tail == HASH_A
            await reader.execute(
                _INSERT,
                uuid4(),
                tenant_id,
                ticket_id,
                uuid4(),
                "OBJ-B",
                new_tail,
                HASH_B,
            )

            rows = await reader.fetch(
                "SELECT prev_hash, hash FROM erp.audit_image "
                "WHERE tenant_id = $1 ORDER BY chain_seq",
                tenant_id,
            )
            assert [row["prev_hash"] for row in rows] == ["", HASH_A]
            assert [row["hash"] for row in rows] == [HASH_A, HASH_B]
        finally:
            await reader.close()
            await writer.close()

    asyncio.run(main())
