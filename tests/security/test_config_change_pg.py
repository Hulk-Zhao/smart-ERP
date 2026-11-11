"""C6 配置变更审计的 PostgreSQL 链路：真写 ``config_change`` 链并验链。

服务层判定（23 条）见 ``test_config_access.py``；这里补 SQL 路径与真实落库，
避免「内存实现绿、SQL 路径空」。全模块只在 ``ERP_TEST_DATABASE_URL`` 存在时运行。

覆盖的边界：

- 未授权角色 / 未登记类别 / 高风险缺复核 → 拒绝且**零写入**；
- 授权成功 → 库内真实行、``config_change`` 与链字段对齐；
- 第二次变更 → ``prev_hash`` 接上一条 ``hash``，整链验过；
- 篡改库内行 → 验链必须在对应 ``chain_seq`` 处报断裂（证明验链不是空跑）；
- 重复提交 → 服务层不静默去重，两次都进链（HTTP 层幂等键另议）。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.security.config_access import (
    ConfigAccessDenied,
    ConfigChange,
    ConfigChangeService,
    ConfigDualReviewRequired,
    ConfigRoleDenied,
)
from apps.security.config_change_chain import (
    SqlConfigChangeRecorder,
    change_payload,
    compute_change_hash,
    verify_config_change_chain,
)
from apps.security.identity import ScopeGrant, UserPrincipal

_DSN = os.environ.get("ERP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="未设置 ERP_TEST_DATABASE_URL，跳过 PG 配置链用例"
)

ORG = "ORG-1"
PERIOD = "2026-10"


def _raw_dsn() -> str:
    return (
        (_DSN or "")
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg://", "postgresql://")
    )


def _upgrade() -> None:
    config = Config(os.path.join(os.getcwd(), "alembic.ini"))
    config.set_main_option("sqlalchemy.url", _DSN or "")
    config.set_main_option(
        "script_location", os.path.join(os.getcwd(), "db", "migrations")
    )
    command.upgrade(config, "head")


def _principal(
    tenant_id: UUID, user_id: UUID, roles: frozenset[str]
) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=roles,
        grant=ScopeGrant(
            org_ids=frozenset({ORG}),
            periods=frozenset({PERIOD}),
            system_ids=frozenset({"u8"}),
        ),
    )


def _change(kind: str = "label") -> ConfigChange:
    return ConfigChange(
        kind=kind,
        target="connector:u8",
        before={"label": "U8 生产"},
        after={"label": "U8 生产（新）"},
    )


def _json(value: Any) -> Any:
    """asyncpg 默认把 json/jsonb 解成字符串，断言前显式解码。"""

    if isinstance(value, str):
        return json.loads(value)
    return value


async def _rows(tenant_id: UUID) -> list[asyncpg.Record]:
    """绕过 ORM 直接读库：库内真实行才是证据。"""

    conn = await asyncpg.connect(_raw_dsn())
    try:
        return await conn.fetch(
            """
            SELECT id, chain_seq, kind, target, before, after, actor_id,
                   approver_id, ticket_id, emergency, prev_hash, hash, created_at
              FROM erp.config_change
             WHERE tenant_id = $1
             ORDER BY chain_seq
            """,
            tenant_id,
        )
    finally:
        await conn.close()


def _row_hash(row: asyncpg.Record, tenant_id: UUID) -> str:
    payload = change_payload(
        change_id=row["id"],
        tenant_id=tenant_id,
        kind=row["kind"],
        target=row["target"],
        before=_json(row["before"]),
        after=_json(row["after"]),
        actor_id=row["actor_id"],
        approver_id=row["approver_id"],
        ticket_id=row["ticket_id"],
        emergency=row["emergency"],
        prev_hash=row["prev_hash"],
        created_at=row["created_at"],
    )
    return compute_change_hash(row["prev_hash"], payload)


def _engine() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_DSN or "")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


def test_authorized_change_lands_in_real_pg_and_chain_verifies() -> None:
    _upgrade()
    tenant_id = uuid4()

    async def main() -> None:
        engine, factory = _engine()
        try:
            service = ConfigChangeService(recorder=SqlConfigChangeRecorder(factory))
            admin = _principal(tenant_id, uuid4(), frozenset({"platform_admin"}))
            result = await service.apply_async(principal=admin, change=_change("label"))

            rows = await _rows(tenant_id)
            assert len(rows) == 1
            row = rows[0]
            assert row["id"] == result.change_id
            assert row["chain_seq"] >= 1
            assert row["prev_hash"] == ""
            assert row["kind"] == "label"
            assert row["actor_id"] == admin.user_id
            assert row["approver_id"] is None
            assert _json(row["before"]) == {"label": "U8 生产"}
            assert _json(row["after"]) == {"label": "U8 生产（新）"}
            assert row["hash"] == _row_hash(row, tenant_id)

            check = await verify_config_change_chain(factory, tenant_id)
            assert check.ok is True
            assert check.checked == 1
        finally:
            await engine.dispose()

    asyncio.run(main())


def test_second_change_links_to_previous_hash() -> None:
    _upgrade()
    tenant_id = uuid4()

    async def main() -> None:
        engine, factory = _engine()
        try:
            service = ConfigChangeService(recorder=SqlConfigChangeRecorder(factory))
            admin = _principal(tenant_id, uuid4(), frozenset({"platform_admin"}))
            await service.apply_async(principal=admin, change=_change("label"))
            await service.apply_async(principal=admin, change=_change("quota"))

            rows = await _rows(tenant_id)
            assert len(rows) == 2
            assert rows[1]["chain_seq"] > rows[0]["chain_seq"]
            assert rows[1]["prev_hash"] == rows[0]["hash"]
            assert rows[1]["hash"] == _row_hash(rows[1], tenant_id)

            check = await verify_config_change_chain(factory, tenant_id)
            assert check.ok is True
            assert check.checked == 2
        finally:
            await engine.dispose()

    asyncio.run(main())


@pytest.mark.parametrize("roles", [frozenset({"auditor"}), frozenset({"operator"})])
def test_unauthorized_role_is_rejected_with_zero_rows(roles: frozenset[str]) -> None:
    _upgrade()
    tenant_id = uuid4()

    async def main() -> None:
        engine, factory = _engine()
        try:
            service = ConfigChangeService(recorder=SqlConfigChangeRecorder(factory))
            user = _principal(tenant_id, uuid4(), roles)
            with pytest.raises(ConfigRoleDenied):
                await service.apply_async(principal=user, change=_change("label"))
            assert await _rows(tenant_id) == []
        finally:
            await engine.dispose()

    asyncio.run(main())


def test_unregistered_kind_is_rejected_with_zero_rows() -> None:
    _upgrade()
    tenant_id = uuid4()

    async def main() -> None:
        engine, factory = _engine()
        try:
            service = ConfigChangeService(recorder=SqlConfigChangeRecorder(factory))
            admin = _principal(tenant_id, uuid4(), frozenset({"platform_admin"}))
            with pytest.raises(ConfigAccessDenied):
                await service.apply_async(
                    principal=admin, change=_change("unregistered_kind")
                )
            assert await _rows(tenant_id) == []
        finally:
            await engine.dispose()

    asyncio.run(main())


def test_high_risk_without_approver_is_rejected_with_zero_rows() -> None:
    _upgrade()
    tenant_id = uuid4()

    async def main() -> None:
        engine, factory = _engine()
        try:
            service = ConfigChangeService(recorder=SqlConfigChangeRecorder(factory))
            admin = _principal(tenant_id, uuid4(), frozenset({"platform_admin"}))
            with pytest.raises(ConfigDualReviewRequired):
                await service.apply_async(principal=admin, change=_change("endpoint"))
            assert await _rows(tenant_id) == []
        finally:
            await engine.dispose()

    asyncio.run(main())


def test_tampered_row_is_detected_by_chain_verification() -> None:
    _upgrade()
    tenant_id = uuid4()

    async def main() -> None:
        engine, factory = _engine()
        try:
            service = ConfigChangeService(recorder=SqlConfigChangeRecorder(factory))
            admin = _principal(tenant_id, uuid4(), frozenset({"platform_admin"}))
            await service.apply_async(principal=admin, change=_change("label"))
            rows = await _rows(tenant_id)
            tampered_seq = rows[0]["chain_seq"]

            conn = await asyncpg.connect(_raw_dsn())
            try:
                # append-only 触发器对所有角色生效；测试以建表身份临时关闭，
                # 模拟「有人绕过应用直接改库」，验证验链能发现。
                await conn.execute(
                    "ALTER TABLE erp.config_change "
                    "DISABLE TRIGGER config_change_append_only"
                )
                try:
                    await conn.execute(
                        "UPDATE erp.config_change SET target = $2 WHERE tenant_id = $1",
                        tenant_id,
                        "connector:tampered",
                    )
                finally:
                    await conn.execute(
                        "ALTER TABLE erp.config_change "
                        "ENABLE TRIGGER config_change_append_only"
                    )
            finally:
                await conn.close()

            check = await verify_config_change_chain(factory, tenant_id)
            assert check.ok is False
            assert check.broken_at == tampered_seq
            assert "疑似篡改" in check.detail
        finally:
            await engine.dispose()

    asyncio.run(main())


def test_duplicate_submission_is_recorded_twice_not_silently_deduped() -> None:
    """服务层没有幂等键：重复提交是两次独立变更，两次都进链。

    去重属于未来 HTTP 入口的 ``Idempotency-Key`` 职责；本期没有对外入口，
    因此这里明确「不静默去重」，避免重试被悄悄吞掉。
    """

    _upgrade()
    tenant_id = uuid4()

    async def main() -> None:
        engine, factory = _engine()
        try:
            service = ConfigChangeService(recorder=SqlConfigChangeRecorder(factory))
            admin = _principal(tenant_id, uuid4(), frozenset({"platform_admin"}))
            first = await service.apply_async(principal=admin, change=_change("label"))
            second = await service.apply_async(principal=admin, change=_change("label"))

            rows = await _rows(tenant_id)
            assert len(rows) == 2
            assert rows[0]["id"] == first.change_id
            assert rows[1]["id"] == second.change_id
            assert rows[1]["prev_hash"] == rows[0]["hash"]

            check = await verify_config_change_chain(factory, tenant_id)
            assert check.ok is True
            assert check.checked == 2
        finally:
            await engine.dispose()

    asyncio.run(main())


def test_sync_apply_refuses_async_recorder_instead_of_dropping_audit() -> None:
    tenant_id = uuid4()

    async def main() -> None:
        engine, factory = _engine()
        try:
            service = ConfigChangeService(recorder=SqlConfigChangeRecorder(factory))
            admin = _principal(tenant_id, uuid4(), frozenset({"platform_admin"}))
            with pytest.raises(TypeError):
                service.apply(principal=admin, change=_change("label"))
            assert await _rows(tenant_id) == []
        finally:
            await engine.dispose()

    asyncio.run(main())
