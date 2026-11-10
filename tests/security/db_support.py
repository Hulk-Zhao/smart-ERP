"""权限用例的 PG 连接夹具：以真实身份连库，不做 ``SET ROLE``。

只有 ``erp_app`` **不是表的 owner** 时，迁移里的 REVOKE 才有意义，所以这里：
1. 用 ``ERP_TEST_DATABASE_URL``（建表身份，CI 是 ``erp_test``）做 admin 连接；
2. 以 ``erp_app`` 真实连接（``current_user = session_user = erp_app``）跑负向断言；
3. 缺角色或权限不足以设临时口令时**带原因 skip**——CI 另有步骤断言 ``skipped == 0``，
   所以 skip 不会被当成通过。
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

APP_ROLE = "erp_app"

# 0002 里显式 REVOKE UPDATE/DELETE/TRUNCATE 的四张审计表
AUDIT_TABLES = ("audit_image", "audit_anchor", "approval_record", "config_change")

# mark_step 真正会写的列；其余列即使有行也必须不可 UPDATE
STEP_MUTABLE_COLUMNS = (
    "state",
    "response_snapshot",
    "error_code",
    "latency_ms",
    "before_ref",
    "after_ref",
)

SKIP_REASON = "未设置 ERP_TEST_DATABASE_URL，跳过 PG 权限用例"


def has_pg() -> bool:
    return bool(_ADMIN_DSN or _APP_DSN)


def normalize(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


def run_async[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


async def admin_conn() -> asyncpg.Connection:
    if not _ADMIN_DSN:
        pytest.skip("未设置 ERP_TEST_DATABASE_URL，无法确认 owner 身份")
    return await asyncpg.connect(normalize(_ADMIN_DSN))


async def app_dsn() -> str:
    """拿到一条可登录的 erp_app 连接串（真实身份，不是 SET ROLE）。"""

    if _APP_DSN:
        return normalize(_APP_DSN)

    assert _ADMIN_DSN is not None
    admin = await admin_conn()
    try:
        role_exists = await admin.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", APP_ROLE
        )
        if not role_exists:
            pytest.skip(
                f"库中不存在 {APP_ROLE}：它必须在跑迁移之前创建，否则迁移里的 REVOKE 会被跳过"
            )
        can_alter = await admin.fetchval(
            "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user"
        )
        if not can_alter:
            pytest.skip("当前角色无 SUPERUSER/CREATEROLE，无法为 erp_app 设临时口令")
        # 只改测试库里的应用角色口令；生产口令不经过这条路径。
        await admin.execute(f"ALTER ROLE {APP_ROLE} LOGIN PASSWORD '{_APP_PASSWORD}'")
        # 与 00-provision.sql §7 对齐：否则负向用例会因为「schema 不可用」
        # 而不是「表不可写」报错，证据就不成立了。
        await admin.execute(f"GRANT USAGE ON SCHEMA erp TO {APP_ROLE}")
    finally:
        await admin.close()

    parts = urlsplit(normalize(_ADMIN_DSN))
    netloc = f"{APP_ROLE}:{quote(_APP_PASSWORD, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def app_conn() -> asyncpg.Connection:
    conn = await asyncpg.connect(await app_dsn())
    current = await conn.fetchval("SELECT current_user")
    session = await conn.fetchval("SELECT session_user")
    assert (current, session) == (APP_ROLE, APP_ROLE), (
        f"连接身份不是 {APP_ROLE}（current_user={current}, session_user={session}），"
        "负向断言会失去意义"
    )
    return conn
