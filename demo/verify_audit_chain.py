#!/usr/bin/env python3
"""从库里读回审计链，用生产函数逐条复验。

与 ``tests/demo/test_seed_audit_chain.py`` 的分工：

- 那个测试校验**种子 JSON**（内存重算），防止生成器把 hash 算错；
- 本脚本校验**已在 PG 里的数据**（``erp.audit_image`` → ``SqlAuditStore`` →
  ``AuditLedger.verify``），与演示栈打开工单详情时走的是同一条读路径。
  时间戳经过 ``timestamptz`` 往返后若与种子口径不一致，这里会直接失败。

用法（Python 3.12）：

    python demo/verify_audit_chain.py --dsn postgresql://... --all
    python demo/verify_audit_chain.py --dsn postgresql://... --scenario fmcg-distribution

成功时退出码为 0，任一场景验链失败或库里条数与种子不符则退出码为 1。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from apps.audit.ledger import AuditLedger  # noqa: E402
from apps.audit.store import SqlAuditStore  # noqa: E402

SCEN_DIR = ROOT / "scenarios"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def seed_tenant(scenario: str) -> tuple[UUID, int]:
    """从种子取租户与应校验条数：库里条数对不上同样算失败。"""

    path = SCEN_DIR / scenario / "erp_seed.json"
    if not path.exists():
        fail(f"场景 {scenario} 未生成，请先运行 demo/tools/build_datasets.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    images = payload.get("audit_images") or []
    if not images:
        fail(f"场景 {scenario} 种子里没有审计影像")
    expected = len(images)
    tenants = {row["tenant_id"] for row in images}
    if len(tenants) != 1:
        fail(f"场景 {scenario} 的种子跨越多个 tenant：{sorted(tenants)}")
    return UUID(tenants.pop()), expected


def async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    return dsn


async def verify_all(dsn: str, scenarios: list[str]) -> int:
    engine = create_async_engine(async_dsn(dsn))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    ledger = AuditLedger(store=SqlAuditStore(session_factory))
    failed = 0
    total = 0
    try:
        for scenario in scenarios:
            tenant_id, expected = seed_tenant(scenario)
            result = await ledger.verify(tenant_id)
            total += result.checked
            status = "ok" if result.ok and result.checked == expected else "FAIL"
            if status == "FAIL":
                failed += 1
            detail = ""
            if not result.ok:
                detail = f" broken_at={result.broken_at} {result.detail}"
            elif result.checked != expected:
                detail = f" 期望 {expected} 条，库里 {result.checked} 条（漏灌或多灌）"
            print(
                f"[{status}] {scenario} tenant={tenant_id} "
                f"checked={result.checked}/{expected}{detail}"
            )
    finally:
        await engine.dispose()
    print(f"TOTAL images={total} tenants_failed={failed}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 PostgreSQL 读回审计影像，用生产 AuditLedger 复验哈希链"
    )
    parser.add_argument("--scenario", action="append", help="场景目录名，可重复")
    parser.add_argument("--all", action="store_true", help="校验全部场景（默认）")
    parser.add_argument("--dsn", help="PostgreSQL DSN；也可用 DEMO_DSN")
    args = parser.parse_args()

    if not args.all and not args.scenario:
        args.scenario = None
    scenarios = args.scenario or sorted(
        path.name for path in SCEN_DIR.iterdir() if (path / "erp_seed.json").exists()
    )
    if not scenarios:
        fail("demo/scenarios 下没有可用场景，请先运行生成器")

    dsn = args.dsn or os.environ.get("DEMO_DSN")
    if not dsn:
        fail("必须提供 --dsn 或环境变量 DEMO_DSN")
    return asyncio.run(verify_all(dsn, scenarios))


if __name__ == "__main__":
    raise SystemExit(main())
