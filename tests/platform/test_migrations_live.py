"""空库从零迁移到 head 的集成测试（真实 PG15）。

覆盖 S3 报过的阻塞场景：**空库** ``upgrade head`` 必须一次成功（含 revision id
长度与 version 列宽度匹配）。流程：upgrade → downgrade base → upgrade，验证可回退可重放。

两条运行路径：

1. 设置了 ``ERP_TEST_DATABASE_URL``（CI 的 postgres service、本地已有 PG）：复用该实例，
   在它上面建一次性库 ``mig_<uuid>``，把 ``ERP_DATABASE_URL`` 指过去，跑完
   ``DROP DATABASE ... WITH (FORCE)``。**此路径不允许 skip**：连不上、建库失败、
   库没删掉都是红——否则「迁移链从未在 CI 真正执行」会再次变成静默。
2. 未设置（本机没有 PG 的人）：退回 docker 起一次性 ``postgres:15-alpine`` 容器；
   环境缺失时跳过。docker 路径不参与 CI 门禁，CI 走的是路径 1。
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
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from apps.platform.contracts.export_contracts import ROOT
from tests.platform.test_migrations import BASELINE_TABLES, VERSION_TABLE

DSN_ENV = "ERP_TEST_DATABASE_URL"
IMAGE = os.environ.get("ERP_PG_TEST_IMAGE", "postgres:15-alpine")
STARTUP_TIMEOUT_S = 60.0


def _admin_dsn() -> str | None:
    value = os.environ.get(DSN_ENV, "").strip()
    return value or None


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


def _skip_reason() -> str | None:
    """只有「没有 DSN 且 docker 不可用」才允许 skip；有 DSN 一律不跳。"""

    if _admin_dsn() is not None:
        return None
    if _docker_ready():
        return None
    return (
        f"未设置 {DSN_ENV}，且本机缺少可用的 docker 或镜像 {IMAGE}"
        "（镜像可用 ERP_PG_TEST_IMAGE 覆盖）"
    )


_SKIP_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "环境缺失")


def _asyncpg_dsn(dsn: str) -> str:
    return make_url(dsn).set(drivername="postgresql").render_as_string(hide_password=False)


def _with_database(dsn: str, database: str) -> str:
    return make_url(dsn).set(database=database).render_as_string(hide_password=False)


def _execute(dsn: str, sql: str) -> None:
    async def run() -> None:
        conn = await asyncpg.connect(_asyncpg_dsn(dsn))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run())


def _fetch(dsn: str, sql: str) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        conn = await asyncpg.connect(_asyncpg_dsn(dsn))
        try:
            return [dict(row) for row in await conn.fetch(sql)]
        finally:
            await conn.close()

    return asyncio.run(run())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _scratch_database(admin_dsn: str) -> Iterator[str]:
    """在已有 PG 实例上建一次性库，跑完强制删除；删除结果也要断言。"""

    name = f"mig_{uuid.uuid4().hex}"
    _execute(admin_dsn, f'CREATE DATABASE "{name}"')
    exists = _fetch(admin_dsn, f"SELECT 1 AS ok FROM pg_database WHERE datname = '{name}'")
    assert exists, f"一次性迁移库 {name} 创建后查不到"
    print(f"[migrations-live] 已创建一次性迁移库 {name}")
    try:
        yield _with_database(admin_dsn, name)
    finally:
        _execute(admin_dsn, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        remaining = _fetch(
            admin_dsn, f"SELECT count(*) AS n FROM pg_database WHERE datname = '{name}'"
        )[0]["n"]
        print(f"[migrations-live] 已销毁一次性迁移库 {name}（残留 {remaining}）")
        assert remaining == 0, f"一次性迁移库 {name} 未被销毁"


def _docker_database() -> Iterator[str]:
    """无 DSN 时的退路：起一次性容器，端口由内核分配，避免与固定端口冲突。"""

    container = f"smart-erp-pg-test-{uuid.uuid4().hex[:8]}"
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
            # 强制走 TCP：官方镜像初始化阶段的临时实例只监听 unix socket，
            # 只探 pg_isready 会在 init 期间误判就绪，随后连接被掐断。
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
        yield f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{port}/smart_erp"
    finally:
        _run("docker", "rm", "-f", container)


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """隔离库的连接串：有 DSN 复用 service，无 DSN 退 docker。"""

    admin_dsn = _admin_dsn()
    if admin_dsn is not None:
        yield from _scratch_database(admin_dsn)
        return
    yield from _docker_database()


def _erp_tables(dsn: str) -> set[str]:
    rows = _fetch(dsn, "SELECT tablename FROM pg_tables WHERE schemaname = 'erp'")
    return {f"erp.{row['tablename']}" for row in rows}


def _erp_function_count(dsn: str) -> int:
    rows = _fetch(
        dsn,
        "SELECT count(*) AS n FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'erp'",
    )
    return int(rows[0]["n"])


def _erp_custom_type_count(dsn: str) -> int:
    rows = _fetch(
        dsn,
        "SELECT count(*) AS n FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE n.nspname = 'erp' AND t.typtype IN ('e', 'd')",
    )
    return int(rows[0]["n"])


def _version(dsn: str) -> str:
    rows = _fetch(dsn, f"SELECT version_num FROM erp.{VERSION_TABLE}")
    assert len(rows) == 1, f"版本表 erp.{VERSION_TABLE} 应当恰好一行，实际 {len(rows)} 行"
    return str(rows[0]["version_num"])


def _version_column_width(dsn: str) -> int:
    rows = _fetch(
        dsn,
        "SELECT character_maximum_length AS width FROM information_schema.columns"
        f" WHERE table_schema = 'erp' AND table_name = '{VERSION_TABLE}'"
        " AND column_name = 'version_num'",
    )
    assert len(rows) == 1, f"找不到 erp.{VERSION_TABLE}.version_num"
    width = rows[0]["width"]
    assert isinstance(width, int) and width > 0, f"版本列宽度异常: {width}"
    return width


def test_empty_database_upgrade_downgrade_upgrade(migrated_database, monkeypatch) -> None:
    url = migrated_database
    monkeypatch.setenv("ERP_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    assert expected_head, "alembic.ini 解析不出 head"
    version_table = f"erp.{VERSION_TABLE}"

    command.upgrade(config, "head")
    version = _version(url)
    width = _version_column_width(url)
    # 版本列是 VARCHAR(32)：revision id 超宽会让空库 upgrade 直接 value too long。
    print(f"[migrations-live] upgrade head={version} len(version_num)={len(version)} column_width={width}")
    assert len(version) <= 32, f"version_num 长度 {len(version)} 超过 VARCHAR(32): {version}"
    assert len(version) <= width, f"version_num 长度 {len(version)} 超过实际列宽 {width}: {version}"
    assert version == expected_head, f"空库 upgrade 后停在 {version}，预期 head 是 {expected_head}"

    tables = _erp_tables(url)
    # 只断言「基线表一张不少」：新增流的表由 test_migrations 的注册表差集负责精确比对，
    # 这里写等号会让每个新流都得改本文件。
    missing = (BASELINE_TABLES | {version_table}) - tables
    assert not missing, f"空库 upgrade 后缺表: {sorted(missing)}"
    assert _erp_function_count(url) > 0, "upgrade 后 erp schema 里没有任何函数"

    command.downgrade(config, "base")
    residual_tables = _erp_tables(url) - {version_table}
    residual_functions = _erp_function_count(url)
    residual_types = _erp_custom_type_count(url)
    print(
        f"[migrations-live] downgrade base 后 erp 残留：表 {sorted(residual_tables)}、"
        f"函数 {residual_functions}、枚举/域类型 {residual_types}"
    )
    assert not residual_tables, f"downgrade base 后仍残留表: {sorted(residual_tables)}"
    assert residual_functions == 0, "downgrade base 后 erp schema 里仍残留函数"
    assert residual_types == 0, "downgrade base 后 erp schema 里仍残留枚举/域类型"

    command.upgrade(config, "head")
    replayed = _version(url)
    assert replayed == expected_head, f"重放 upgrade head 后版本 {replayed} 与预期 {expected_head} 不一致"
    assert _erp_tables(url) == tables, "重放 upgrade 后表集合与首次不一致"
