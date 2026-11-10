"""空库从零迁移到 head 的集成测试（真实 PG15 容器）。

覆盖 S3 报过的阻塞场景：**空库** ``upgrade head`` 必须一次成功（含 revision id
长度与 version 列宽度匹配）。流程：upgrade → downgrade base → upgrade，验证可回退可重放。

环境缺失（无 docker / 无镜像 / daemon 未启动）时整条跳过，不让本地与 CI 全红；
端口由内核分配空闲端口，避免与 55432 / 55433 / 15432 等固定端口互相踩。
"""

from __future__ import annotations

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

from apps.platform.contracts.export_contracts import ROOT
from tests.platform.test_migrations import BASELINE_TABLES, VERSION_TABLE

IMAGE = os.environ.get("ERP_PG_TEST_IMAGE", "postgres:15-alpine")
STARTUP_TIMEOUT_S = 60.0


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


@pytest.fixture(scope="module")
def postgres_container() -> Iterator[tuple[str, str]]:
    """(容器名, 连接串)。端口由内核分配，避免与固定端口冲突。"""

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
        yield container, f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{port}/smart_erp"
    finally:
        _run("docker", "rm", "-f", container)


def _erp_tables(container_name: str) -> set[str]:
    rows = _psql(
        container_name,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'erp' ORDER BY tablename",
    )
    return {f"erp.{row}" for row in rows.splitlines() if row}


def test_empty_database_upgrade_downgrade_upgrade(postgres_container, monkeypatch) -> None:
    container, url = postgres_container
    monkeypatch.setenv("ERP_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))

    command.upgrade(config, "head")
    version = _psql(container, f"SELECT version_num FROM erp.{VERSION_TABLE}")
    assert version, "空库 upgrade 后版本表为空"
    assert _erp_tables(container) == BASELINE_TABLES | {f"erp.{VERSION_TABLE}"}
    assert _psql(
        container,
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'erp'",
    ) != "0"

    command.downgrade(config, "base")
    assert _erp_tables(container) == {f"erp.{VERSION_TABLE}"}

    command.upgrade(config, "head")
    assert _psql(container, f"SELECT version_num FROM erp.{VERSION_TABLE}") == version
