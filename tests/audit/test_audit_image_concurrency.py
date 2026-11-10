"""``audit_image`` 并发追加：唯一约束 + 重试让两个写入者串成一条链，而不是分叉。

两条证据缺一不可：

1. **约束行为**（frozen snapshot）：两个会话读到同一个链尾后同时追加，唯一约束
   ``uq_audit_image_parent (tenant_id, prev_hash)`` 拒绝后者；
2. **真实并发路径**：两个独立连接各自走 ``AuditLedger.append_image``，
   一个直接成功、另一个被 ``IntegrityError`` 挡下后重读链尾接在后面。
   单线程 + 人工造数据测不到这一支。

第二条用 ``asyncio.Barrier`` 卡住两边的首次读链尾，确保它们拿到同一个
``prev_hash``，不依赖 sleep 抢时序。
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from apps.audit.ledger import AuditLedger
from apps.audit.store import SqlAuditStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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

_DUP_PARENTS = """
SELECT count(*) FROM (
    SELECT 1 FROM erp.audit_image
     WHERE tenant_id = $1
     GROUP BY tenant_id, prev_hash
    HAVING count(*) > 1
) dup
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
    """约束层：旧快照 + 同一 prev_hash → 恰好一条 UniqueViolation。"""

    tenant_id, ticket_id = _seed_ticket()

    async def main() -> None:
        reader = await asyncpg.connect(_dsn())
        writer = await asyncpg.connect(_dsn())
        try:
            await reader.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            tail = await reader.fetchval(_TAIL, tenant_id)
            assert tail is None, "用例要求空链，便于固定 prev_hash=''"

            await writer.execute(
                _INSERT, uuid4(), tenant_id, ticket_id, uuid4(), "OBJ-A", "", HASH_A
            )

            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await reader.execute(
                    _INSERT, uuid4(), tenant_id, ticket_id, uuid4(), "OBJ-B", "", HASH_B
                )
            await reader.execute("ROLLBACK")

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


class _GatedStore(SqlAuditStore):
    """首次读链尾时等另一条连接，确保两边拿到同一个 prev_hash。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        barrier: asyncio.Barrier,
        counter: dict[str, int],
    ) -> None:
        super().__init__(session_factory)
        self._barrier = barrier
        self._counter = counter
        self._gated = False

    async def last_hash(self, tenant_id: UUID) -> str:
        value = await super().last_hash(tenant_id)
        if not self._gated:
            self._gated = True
            await self._barrier.wait()
        return value

    async def append(self, image) -> None:
        self._counter["appends"] += 1
        await super().append(image)


def test_concurrent_ledger_append_retries_and_keeps_single_chain() -> None:
    """真实并发路径：一个成功、一个撞约束后重接，链仍是一条。"""

    tenant_id, ticket_id = _seed_ticket()

    async def main() -> None:
        assert _DSN is not None
        engines = [create_async_engine(_DSN) for _ in range(2)]
        barrier = asyncio.Barrier(2)
        counter = {"appends": 0}
        ledgers = [
            AuditLedger(
                store=_GatedStore(
                    async_sessionmaker(engine, expire_on_commit=False), barrier, counter
                )
            )
            for engine in engines
        ]

        async def append(ledger: AuditLedger, object_id: str):
            return await ledger.append_image(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                step_id=uuid4(),
                system="fake",
                object_type="invoice_draft",
                object_id=object_id,
                before={"amount": "1.00"},
                after={"amount": "2.00"},
            )

        try:
            results = await asyncio.gather(
                append(ledgers[0], "OBJ-A"),
                append(ledgers[1], "OBJ-B"),
                return_exceptions=True,
            )
            errors = [item for item in results if isinstance(item, BaseException)]
            assert not errors, f"并发追加不应把分叉抛给调用方：{errors}"

            chain = await ledgers[0].store.chain(tenant_id)
            assert len(chain) == 2, "两条并发追加都应落库"
            assert chain[0].prev_hash == ""
            assert chain[1].prev_hash == chain[0].hash, (
                "后者必须重读链尾后接在第一条之后"
            )

            verification = await ledgers[0].verify(tenant_id)
            assert verification.ok is True, "从头重放必须哈希连续"
            assert verification.checked == 2

            # 两次成功写入 + 一次被唯一约束拒绝 = 3 次 append 调用：
            # 证明 IntegrityError 分支真的被走到，而不是两条串行写入。
            assert counter["appends"] == 3, (
                f"append 调用次数={counter['appends']}，期望 3（2 成功 + 1 次约束撞车重试）"
            )

            conn = await asyncpg.connect(_dsn())
            try:
                duplicates = await conn.fetchval(_DUP_PARENTS, tenant_id)
            finally:
                await conn.close()
            assert duplicates == 0, "同父分叉组数必须为 0"
        finally:
            for engine in engines:
                await engine.dispose()

    asyncio.run(main())
