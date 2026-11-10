"""审计表不可被应用角色改写：以真实 ``erp_app`` 连接身份跑的负向验证。

背景：``0002`` 对 ``audit_image`` / ``audit_anchor`` / ``approval_record`` /
``config_change`` 执行了 ``REVOKE UPDATE, DELETE, TRUNCATE``，``00-provision.sql``
又用 ``ALTER DEFAULT PRIVILEGES`` 给新表默认放开 ``UPDATE, DELETE``。也就是说
「审计表不可改」此前只有迁移语句，没有任何运行证据；新增一张审计类表漏一条
REVOKE 就会静默回到可改状态。

**连接身份是这条用例的全部要点**：只有 ``erp_app`` 不是表的 owner，REVOKE 才有意义。
因此这里不用 ``SET ROLE``，而是真的建一条 ``erp_app`` 连接，并在断言前先核对
``current_user = session_user = erp_app``、owner 不是 ``erp_app``——否则「测了等于没测」。

环境要求：
- ``ERP_TEST_DATABASE_URL`` 指向已 ``alembic upgrade head`` 的库，且**建表身份不是
  erp_app**（CI 用 ``erp_test``/``erp_migrator``）；
- ``erp_app`` 角色必须在**跑迁移之前**已存在，否则迁移里的 REVOKE/GRANT 会被跳过
  （CI 的「建应用角色」步骤负责这一步）；
- 若当前连接角色不是 SUPERUSER/CREATEROLE，无法为 ``erp_app`` 设置临时口令，
  用例跳过而不是假装通过。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest

_ADMIN_DSN = os.environ.get("ERP_TEST_DATABASE_URL")
_APP_DSN = os.environ.get("ERP_TEST_APP_DATABASE_URL")
_APP_PASSWORD = os.environ.get("ERP_TEST_APP_PASSWORD", "erp_app_test_only")

pytestmark = pytest.mark.skipif(
    not (_ADMIN_DSN or _APP_DSN),
    reason="未设置 ERP_TEST_DATABASE_URL，跳过 PG 权限用例",
)

AUDIT_TABLES = ("audit_image", "audit_anchor", "approval_record", "config_change")
APP_ROLE = "erp_app"

# mark_step 真正会写的列；其余列即使有行也必须不可 UPDATE
STEP_MUTABLE_COLUMNS = (
    "state",
    "response_snapshot",
    "error_code",
    "latency_ms",
    "before_ref",
    "after_ref",
)
STEP_READONLY_COLUMNS = (
    "request_hash",
    "request_snapshot",
    "idempotency_key",
    "step_no",
)


def _normalize(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


async def _admin_conn() -> asyncpg.Connection:
    if not _ADMIN_DSN:
        pytest.skip("未设置 ERP_TEST_DATABASE_URL，无法确认 owner 身份")
    return await asyncpg.connect(_normalize(_ADMIN_DSN))


async def _app_dsn() -> str:
    """拿到一条可登录的 erp_app 连接串（真实身份，不是 SET ROLE）。"""

    if _APP_DSN:
        return _normalize(_APP_DSN)

    assert _ADMIN_DSN is not None
    admin = await _admin_conn()
    try:
        role_exists = await admin.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", APP_ROLE
        )
        if not role_exists:
            pytest.skip(
                f"库中不存在 {APP_ROLE}：它必须在跑迁移之前创建，否则 REVOKE 被跳过"
            )
        can_alter = await admin.fetchval(
            "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user"
        )
        if not can_alter:
            pytest.skip("当前角色无 SUPERUSER/CREATEROLE，无法为 erp_app 设临时口令")
        # 只改测试库里的应用角色口令；生产口令不经过这条路径。
        await admin.execute(f"ALTER ROLE {APP_ROLE} LOGIN PASSWORD '{_APP_PASSWORD}'")
        # 与 00-provision.sql §7 对齐：应用角色至少要能用 erp schema，
        # 否则负向用例会因为「schema 不可用」而不是「表不可写」报错。
        await admin.execute(f"GRANT USAGE ON SCHEMA erp TO {APP_ROLE}")
    finally:
        await admin.close()

    parts = urlsplit(_normalize(_ADMIN_DSN))
    netloc = f"{APP_ROLE}:{quote(_APP_PASSWORD, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _app_conn() -> asyncpg.Connection:
    conn = await asyncpg.connect(await _app_dsn())
    current = await conn.fetchval("SELECT current_user")
    session = await conn.fetchval("SELECT session_user")
    assert (current, session) == (APP_ROLE, APP_ROLE), (
        f"连接身份不是 {APP_ROLE}（current_user={current}, session_user={session}），"
        "负向断言会失去意义"
    )
    return conn


def _denied(statement: str) -> None:
    async def main() -> None:
        conn = await _app_conn()
        try:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(statement)
        finally:
            await conn.close()

    _run(main())


def test_app_role_is_not_the_owner_of_audit_tables() -> None:
    """owner 必须是建表身份（erp_migrator/erp_test），否则 REVOKE 对它无效。"""

    async def main() -> None:
        conn = await _admin_conn()
        try:
            for table in AUDIT_TABLES + ("ticket_step", "credential_lease"):
                owner = await conn.fetchval(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class "
                    "WHERE oid = to_regclass($1)",
                    f"erp.{table}",
                )
                assert owner is not None, f"erp.{table} 不存在，迁移未执行"
                assert owner != APP_ROLE, (
                    f"erp.{table} 的 owner 是 {APP_ROLE}，REVOKE 对它无效；"
                    "迁移必须以 erp_migrator/erp_test 身份执行"
                )
        finally:
            await conn.close()

    _run(main())


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_app_role_only_has_select_and_insert(table: str) -> None:
    async def main() -> None:
        conn = await _app_conn()
        try:
            for priv in ("SELECT", "INSERT"):
                granted = await conn.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    APP_ROLE,
                    f"erp.{table}",
                    priv,
                )
                assert granted, f"{APP_ROLE} 对 erp.{table} 缺少 {priv}"
            for priv in ("UPDATE", "DELETE", "TRUNCATE"):
                granted = await conn.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    APP_ROLE,
                    f"erp.{table}",
                    priv,
                )
                assert not granted, f"{APP_ROLE} 仍持有 erp.{table} 的 {priv}"
        finally:
            await conn.close()

    _run(main())


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_app_role_update_is_denied(table: str) -> None:
    # 零行也走权限检查；不依赖各表列名是否一致。
    _denied(f"SELECT ctid FROM erp.{table} FOR UPDATE")


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_app_role_delete_is_denied(table: str) -> None:
    _denied(f"DELETE FROM erp.{table} WHERE false")


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_app_role_truncate_is_denied(table: str) -> None:
    _denied(f"TRUNCATE erp.{table}")


def test_ticket_step_update_is_column_scoped() -> None:
    """``ticket_step`` 是可变执行投影：只允许改 mark_step 会写的那六列。"""

    async def main() -> None:
        conn = await _app_conn()
        try:
            table_level = await conn.fetchval(
                "SELECT has_table_privilege($1, $2, 'UPDATE')",
                APP_ROLE,
                "erp.ticket_step",
            )
            assert not table_level, f"{APP_ROLE} 仍持有 erp.ticket_step 的整表 UPDATE"
            for column in STEP_MUTABLE_COLUMNS:
                granted = await conn.fetchval(
                    "SELECT has_column_privilege($1, $2, $3, 'UPDATE')",
                    APP_ROLE,
                    "erp.ticket_step",
                    column,
                )
                assert granted, f"{APP_ROLE} 缺少 erp.ticket_step.{column} 的 UPDATE"
            for column in STEP_READONLY_COLUMNS:
                granted = await conn.fetchval(
                    "SELECT has_column_privilege($1, $2, $3, 'UPDATE')",
                    APP_ROLE,
                    "erp.ticket_step",
                    column,
                )
                assert not granted, f"{APP_ROLE} 不应能改 erp.ticket_step.{column}"
        finally:
            await conn.close()

    _run(main())


def test_ticket_step_mutation_outside_allowed_columns_is_denied() -> None:
    _denied("UPDATE erp.ticket_step SET request_hash = request_hash WHERE false")
    _denied("DELETE FROM erp.ticket_step WHERE false")
    _denied("TRUNCATE erp.ticket_step")


def test_credential_lease_update_is_denied() -> None:
    """租约表当前没有吊销列，代码也没有 UPDATE 路径 → 整表收回。"""

    async def main() -> None:
        conn = await _app_conn()
        try:
            for priv in ("UPDATE", "DELETE", "TRUNCATE"):
                granted = await conn.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    APP_ROLE,
                    "erp.credential_lease",
                    priv,
                )
                assert not granted, f"{APP_ROLE} 仍持有 erp.credential_lease 的 {priv}"
            for priv in ("SELECT", "INSERT"):
                granted = await conn.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    APP_ROLE,
                    "erp.credential_lease",
                    priv,
                )
                assert granted, f"{APP_ROLE} 对 erp.credential_lease 缺少 {priv}"
        finally:
            await conn.close()

    _run(main())
