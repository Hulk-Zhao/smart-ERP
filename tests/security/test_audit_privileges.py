"""审计表不可被应用角色改写：以真实 ``erp_app`` 连接身份跑的负向验证。

背景：``0002`` 对 ``audit_image`` / ``audit_anchor`` / ``approval_record`` /
``config_change`` 执行了 ``REVOKE UPDATE, DELETE, TRUNCATE``，``00-provision.sql``
又用 ``ALTER DEFAULT PRIVILEGES`` 给新表默认放开 ``UPDATE, DELETE``。也就是说
「审计表不可改」此前只有迁移语句，没有任何运行证据。

**连接身份是这条用例的全部要点**：只有 ``erp_app`` 不是表的 owner，REVOKE 才有意义。
因此这里不用 ``SET ROLE``，而是真的建一条 ``erp_app`` 连接，并在断言前先核对
``current_user = session_user = erp_app``、owner 不是 ``erp_app``——否则「测了等于没测」。

环境要求：``ERP_TEST_DATABASE_URL`` 指向已 ``alembic upgrade head`` 的库，
且 ``erp_app`` 角色在**跑迁移之前**已存在（CI 的「建应用角色」步骤负责）。
缺角色时带原因 skip；CI 另有步骤断言 ``skipped == 0``，所以 skip 不会被当成通过。
"""

from __future__ import annotations

import asyncpg
import pytest
from db_support import (
    APP_ROLE,
    AUDIT_TABLES,
    STEP_MUTABLE_COLUMNS,
    admin_conn,
    app_conn,
    has_pg,
    run_async,
)

pytestmark = pytest.mark.skipif(
    not has_pg(), reason="未设置 ERP_TEST_DATABASE_URL，跳过 PG 权限用例"
)

STEP_READONLY_COLUMNS = (
    "request_hash",
    "request_snapshot",
    "idempotency_key",
    "step_no",
)


def _denied(statement: str) -> None:
    async def main() -> None:
        conn = await app_conn()
        try:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(statement)
        finally:
            await conn.close()

    run_async(main())


def test_app_role_is_not_the_owner_of_audit_tables() -> None:
    """owner 必须是建表身份（erp_migrator/erp_test），否则 REVOKE 对它无效。"""

    async def main() -> None:
        conn = await admin_conn()
        try:
            for table in AUDIT_TABLES + ("ticket_step", "credential_lease"):
                owner = await conn.fetchval(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = to_regclass($1)",
                    f"erp.{table}",
                )
                assert owner is not None, f"erp.{table} 不存在，迁移未执行"
                assert owner != APP_ROLE, (
                    f"erp.{table} 的 owner 是 {APP_ROLE}，REVOKE 对它无效；"
                    "迁移必须以 erp_migrator/erp_test 身份执行"
                )
        finally:
            await conn.close()

    run_async(main())


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_app_role_only_has_select_and_insert(table: str) -> None:
    async def main() -> None:
        conn = await app_conn()
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

    run_async(main())


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
        conn = await app_conn()
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

    run_async(main())


def test_ticket_step_mutation_outside_allowed_columns_is_denied() -> None:
    _denied("UPDATE erp.ticket_step SET request_hash = request_hash WHERE false")
    _denied("DELETE FROM erp.ticket_step WHERE false")
    _denied("TRUNCATE erp.ticket_step")


def test_credential_lease_update_is_denied() -> None:
    """租约失效靠读时比较 ``expires_at``，不写库 → 整表收回 UPDATE/DELETE。"""

    async def main() -> None:
        conn = await app_conn()
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

    run_async(main())
