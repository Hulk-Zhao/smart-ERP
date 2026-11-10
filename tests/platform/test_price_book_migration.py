"""0008 计量价格表迁移的真实 PG15 用例：owner / 权限 / 约束 / 可回退。

为什么必须真跑：``REVOKE`` 是否有效取决于「表 owner 是不是执行迁移的角色」，
以及库里是否存在 ``ALTER DEFAULT PRIVILEGES`` 的自动授权。离线 SQL 里看到
``REVOKE`` 字样证明不了任何事——默认授权没还原时 REVOKE 是空操作，用例会假绿。

因此夹具按 ``deploy/smart-erp/db/00-provision.sql`` 还原：
建 ``erp_app`` / ``erp_migrator`` 两个角色 → 给 erp_migrator 建库权限 →
``ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator ... GRANT SELECT, INSERT, UPDATE, DELETE``
→ 再用 **erp_migrator 的连接串**执行 ``alembic upgrade head``。

覆盖点：
1. ``pg_tables.tableowner = 'erp_migrator'``；
2. ``erp_app`` 的 UPDATE / DELETE / TRUNCATE 均 ``permission denied``；
3. ``erp_app`` 的 SELECT / INSERT 可用（否则应用写不进价目）；
4. 跨租户 ``supersedes_id`` 与 ``recon_batch.price_book_id`` 被复合外键拒绝；
5. ``currency`` 格式、自指 supersede、非正单价被 CHECK 拒绝；
6. ``downgrade -1`` 后价格表与四个计价列消失（已快照的计价依据随之丢失），
   再 ``upgrade head`` 可恢复结构。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from apps.platform.contracts.export_contracts import ROOT

IMAGE = os.environ.get("ERP_PG_TEST_IMAGE", "postgres:15-alpine")
STARTUP_TIMEOUT_S = 60.0

REVISION = "0008_metering_price_book"
PREVIOUS_REVISION = "0007_recon_batch_projection"
PRICE_COLUMNS = (
    "currency",
    "price_book_id",
    "unit_price",
    "unit_price_source",
)

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
APPROVER = "33333333-3333-3333-3333-333333333333"

# 还原 00-provision.sql 的关键部分：没有这段默认授权，REVOKE 就是空操作。
PROVISION_SQL = """
CREATE ROLE erp_app LOGIN PASSWORD 'erp_app';
CREATE ROLE erp_migrator LOGIN PASSWORD 'erp_migrator';
GRANT CONNECT, CREATE ON DATABASE smart_erp TO erp_migrator;
GRANT CONNECT ON DATABASE smart_erp TO erp_app;
CREATE SCHEMA IF NOT EXISTS erp AUTHORIZATION erp_migrator;
ALTER ROLE erp_migrator SET search_path = erp;
ALTER ROLE erp_app SET search_path = erp;
GRANT USAGE ON SCHEMA erp TO erp_app;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA erp
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO erp_app;
"""

VERSION_SQL = "SELECT version_num FROM erp.alembic_version_erp"

PRICE_COLUMNS_SQL = """
SELECT string_agg(column_name, ',' ORDER BY column_name)
  FROM information_schema.columns
 WHERE table_schema = 'erp'
   AND table_name = 'recon_batch'
   AND column_name IN ('unit_price', 'currency', 'unit_price_source', 'price_book_id')
"""


def _run(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    if _run("docker", "info", timeout=20).returncode != 0:
        return False
    return _run("docker", "image", "inspect", IMAGE, timeout=20).returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_ready(),
    reason=f"需要可用的 docker 与本地镜像 {IMAGE}（可用 ERP_PG_TEST_IMAGE 覆盖）",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _psql(container: str, sql: str) -> str:
    result = _run(
        "docker", "exec", container,
        "psql", "-U", "postgres", "-d", "smart_erp", "-tA", "-c", sql,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


async def _execute(url: str, sql: str) -> None:
    # AUTOCOMMIT：权限/约束失败后语句立即结束，不会把连接留成失败事务
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def _fetch(url: str, sql: str) -> list[tuple]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            return [tuple(row) for row in result.all()]
    finally:
        await engine.dispose()


def run_sql(url: str, sql: str) -> None:
    asyncio.run(_execute(url, sql))


def fetch(url: str, sql: str) -> list[tuple]:
    return asyncio.run(_fetch(url, sql))


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="module")
def provisioned_pg() -> Iterator[tuple[str, str, str]]:
    """(容器名, erp_migrator 连接串, erp_app 连接串)，库已 upgrade 到 head。"""

    container = f"smart-erp-pg-price-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    started = _run(
        "docker", "run", "-d", "--rm",
        "--name", container,
        "-e", "POSTGRES_PASSWORD=postgres",
        "-e", "POSTGRES_DB=smart_erp",
        "-p", f"127.0.0.1:{port}:5432",
        IMAGE,
    )
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            # 强制走 TCP：初始化阶段的临时实例只监听 unix socket，会误判就绪
            ready = _run(
                "docker", "exec", container,
                "psql", "-U", "postgres", "-d", "smart_erp", "-h", "127.0.0.1",
                "-tAc", "SELECT 1",
            )
            if ready.returncode == 0 and ready.stdout.strip() == "1":
                break
            time.sleep(0.3)
        else:
            raise AssertionError(f"PG15 容器 {container} 未在 {STARTUP_TIMEOUT_S}s 内就绪")

        _psql(container, PROVISION_SQL)
        migrator_url = (
            f"postgresql+asyncpg://erp_migrator:erp_migrator@127.0.0.1:{port}/smart_erp"
        )
        app_url = f"postgresql+asyncpg://erp_app:erp_app@127.0.0.1:{port}/smart_erp"
        # 迁移必须由 erp_migrator 执行，否则表 owner 不是它，REVOKE 静默无效
        command.upgrade(_config(migrator_url), "head")
        yield container, migrator_url, app_url
    finally:
        _run("docker", "rm", "-f", container)


def _price_insert(
    price_id: uuid.UUID,
    tenant_id: str,
    effective_from: str,
    *,
    currency: str = "CNY",
    unit_price: str = "0.3000",
    contract_ref: str = "HT-2026-001",
    supersedes: uuid.UUID | None = None,
) -> str:
    supersedes_sql = f"'{supersedes}'" if supersedes is not None else "NULL"
    return f"""
      INSERT INTO erp.metering_price_book
        (id, tenant_id, currency, unit_price, effective_from, contract_ref,
         approved_by_user_id, approved_by_display, approved_at, supersedes_id)
      VALUES
        ('{price_id}', '{tenant_id}', '{currency}', {unit_price},
         '{effective_from}', '{contract_ref}', '{APPROVER}', '张三',
         '2026-11-01T00:00:00+08:00', {supersedes_sql})
    """


def _batch_insert(
    tenant_id: str,
    price_book_id: uuid.UUID,
    *,
    batch_id: uuid.UUID | None = None,
    fingerprint: str = "fp-price-book",
) -> str:
    batch_id = batch_id or uuid.uuid4()
    return f"""
      INSERT INTO erp.recon_batch
        (id, tenant_id, org_id, period, status, conclusive, status_reason,
         suppliers, system_ids, input_fingerprint, created_by,
         unit_price, currency, unit_price_source, price_book_id)
      VALUES
        ('{batch_id}', '{tenant_id}', 'org-1', '2026-10', 'conclusive', true, 'ok',
         ARRAY[]::text[], ARRAY[]::text[], '{fingerprint}', '{APPROVER}',
         0.3000, 'CNY', 'snapshot', '{price_book_id}')
    """


def test_price_book_owner_is_migrator(provisioned_pg) -> None:
    container, _, _ = provisioned_pg

    owner = _psql(
        container,
        "SELECT tableowner FROM pg_tables"
        " WHERE schemaname = 'erp' AND tablename = 'metering_price_book'",
    )
    assert owner == "erp_migrator", (
        f"表 owner 是 {owner!r}，不是 erp_migrator：REVOKE 对它无效"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE erp.metering_price_book SET unit_price = unit_price",
        "DELETE FROM erp.metering_price_book",
        "TRUNCATE erp.metering_price_book",
    ],
    ids=["update", "delete", "truncate"],
)
def test_app_role_cannot_mutate_price_book(provisioned_pg, sql: str) -> None:
    _, _, app_url = provisioned_pg

    with pytest.raises(DBAPIError) as excinfo:
        run_sql(app_url, sql)
    assert "permission denied" in str(excinfo.value)


def test_app_role_can_select_and_insert(provisioned_pg) -> None:
    _, _, app_url = provisioned_pg

    price_id = uuid.uuid4()
    run_sql(app_url, _price_insert(price_id, TENANT_A, "2026-11-01T00:00:00+08:00"))

    rows = fetch(
        app_url,
        "SELECT unit_price, currency FROM erp.metering_price_book"
        f" WHERE id = '{price_id}'",
    )
    assert str(rows[0][0]) == "0.3000"
    assert str(rows[0][1]).strip() == "CNY"


def test_cross_tenant_supersedes_is_rejected(provisioned_pg) -> None:
    _, migrator_url, _ = provisioned_pg

    foreign_price = uuid.uuid4()
    run_sql(
        migrator_url,
        _price_insert(foreign_price, TENANT_B, "2026-12-01T00:00:00+08:00"),
    )

    with pytest.raises(IntegrityError) as excinfo:
        run_sql(
            migrator_url,
            _price_insert(
                uuid.uuid4(),
                TENANT_A,
                "2026-12-01T00:00:00+08:00",
                supersedes=foreign_price,
            ),
        )
    assert "fk_price_book_supersedes" in str(excinfo.value)


def test_cross_tenant_price_book_on_batch_is_rejected(provisioned_pg) -> None:
    _, migrator_url, _ = provisioned_pg

    foreign_price = uuid.uuid4()
    run_sql(
        migrator_url,
        _price_insert(foreign_price, TENANT_B, "2027-01-01T00:00:00+08:00"),
    )

    with pytest.raises(IntegrityError) as excinfo:
        run_sql(migrator_url, _batch_insert(TENANT_A, foreign_price))
    assert "fk_recon_batch_price_book" in str(excinfo.value)


def test_same_tenant_batch_snapshot_is_accepted(provisioned_pg) -> None:
    _, migrator_url, _ = provisioned_pg

    own_price = uuid.uuid4()
    run_sql(
        migrator_url,
        _price_insert(own_price, TENANT_A, "2027-02-01T00:00:00+08:00"),
    )
    batch_id = uuid.uuid4()
    run_sql(
        migrator_url,
        _batch_insert(
            TENANT_A, own_price, batch_id=batch_id, fingerprint="fp-same-tenant"
        ),
    )

    rows = fetch(
        migrator_url,
        "SELECT unit_price_source, price_book_id, currency FROM erp.recon_batch"
        f" WHERE id = '{batch_id}'",
    )
    assert rows[0][0] == "snapshot"
    assert str(rows[0][1]) == str(own_price)
    assert str(rows[0][2]).strip() == "CNY"


@pytest.mark.parametrize(
    "sql",
    [
        _price_insert(
            uuid.uuid4(), TENANT_A, "2027-03-01T00:00:00+08:00", currency="CN"
        ),
        _price_insert(
            uuid.uuid4(), TENANT_A, "2027-03-02T00:00:00+08:00", currency="cny"
        ),
        _price_insert(
            uuid.uuid4(), TENANT_A, "2027-03-03T00:00:00+08:00", unit_price="0"
        ),
        _price_insert(
            uuid.uuid4(), TENANT_A, "2027-03-04T00:00:00+08:00", contract_ref="   "
        ),
    ],
    ids=["currency_too_short", "currency_lowercase", "price_not_positive", "blank_contract"],
)
def test_invalid_price_rows_are_rejected(provisioned_pg, sql: str) -> None:
    _, migrator_url, _ = provisioned_pg

    with pytest.raises(IntegrityError) as excinfo:
        run_sql(migrator_url, sql)
    assert "violates check constraint" in str(excinfo.value)


def test_self_supersede_is_rejected(provisioned_pg) -> None:
    _, migrator_url, _ = provisioned_pg

    price_id = uuid.uuid4()
    with pytest.raises(IntegrityError) as excinfo:
        run_sql(
            migrator_url,
            _price_insert(
                price_id,
                TENANT_A,
                "2027-04-01T00:00:00+08:00",
                supersedes=price_id,
            ),
        )
    assert "ck_price_book_no_self_supersede" in str(excinfo.value)


def test_downgrade_drops_price_snapshot_and_upgrade_restores(provisioned_pg) -> None:
    container, migrator_url, _ = provisioned_pg

    assert _psql(container, VERSION_SQL) == REVISION
    assert int(_psql(container, "SELECT count(*) FROM erp.metering_price_book")) > 0
    assert _psql(container, PRICE_COLUMNS_SQL) == ",".join(PRICE_COLUMNS)

    config = _config(migrator_url)
    # 回退会 DROP COLUMN，已快照的计价依据随之丢失（迁移 docstring 已写明）
    command.downgrade(config, "-1")

    assert _psql(container, VERSION_SQL) == PREVIOUS_REVISION
    assert (
        _psql(
            container,
            "SELECT count(*) FROM pg_tables"
            " WHERE schemaname = 'erp' AND tablename = 'metering_price_book'",
        )
        == "0"
    )
    assert _psql(container, PRICE_COLUMNS_SQL) == ""

    command.upgrade(config, "head")

    assert _psql(container, VERSION_SQL) == REVISION
    assert _psql(container, PRICE_COLUMNS_SQL) == ",".join(PRICE_COLUMNS)
