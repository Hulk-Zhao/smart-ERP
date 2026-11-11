#!/usr/bin/env python3
"""从库里读回审计链，逐条复验。

判据分两层（2026-11-11 修正，起因见 ``docs/product/tech/流差距卡.md`` 教训区）：

1. **链自洽**（永远跑、永远不能松）：逐条 ``prev_hash == 上一条 hash``；
   首条 ``prev_hash`` 为空；同租户内 ``chain_seq`` 递增；``chain_seq`` 全局唯一。
   这条和能力无关，写路径开不开都必须过。
2. **种子前缀核对**：从种子里取每个租户的 ``(count, tail_hash)``，只比该租户
   **前 count 条**的尾 hash，不拿全链尾去比。多出来的行逐条列出
   （``object_type`` / ``object_id`` / ``chain_seq`` / ``created_at``），标成
   「运行时新增」——既不算失败，也不静默通过。

``--seed-exact``：运行时新增 > 0 即失败，只用于空库 / CI 场景。

> 旧判据「库里条数必须严格等于种子条数」是错的：写路径打开后每次真实写入都会
> 让条数变多。它与 ``verify_against_anchor`` 拿全链尾比区间尾是同一个错误——
> 第二次出现，见教训区。

用法（Python 3.12）：

    python demo/verify_audit_chain.py --dsn postgresql://... --all
    python demo/verify_audit_chain.py --dsn postgresql://... --scenario fmcg-distribution
    python demo/verify_audit_chain.py --dsn postgresql://... --all --seed-exact

退出码：0 = 链自洽且种子前缀吻合（运行时新增允许）；1 = 链断 / 漏灌 / 前缀尾不符 /
``--seed-exact`` 下出现运行时新增；2 = 参数或环境错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCEN_DIR = ROOT / "scenarios"

SQL = text(
    """
    SELECT chain_seq, tenant_id, prev_hash, hash,
           object_type, object_id, created_at
    FROM erp.audit_image
    ORDER BY chain_seq
    """
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def seed_tenant(scenario: str) -> tuple[UUID, int, str]:
    """从种子取租户、应校验条数与前缀尾 hash。"""

    path = SCEN_DIR / scenario / "erp_seed.json"
    if not path.exists():
        fail(f"场景 {scenario} 未生成，请先运行 demo/tools/build_datasets.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    images = payload.get("audit_images") or []
    if not images:
        fail(f"场景 {scenario} 种子里没有审计影像")
    tenants = {row["tenant_id"] for row in images}
    if len(tenants) != 1:
        fail(f"场景 {scenario} 的种子跨越多个 tenant：{sorted(tenants)}")
    return UUID(tenants.pop()), len(images), images[-1]["hash"]


def async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    return dsn


async def fetch_rows(dsn: str) -> list:
    engine = create_async_engine(async_dsn(dsn))
    try:
        async with engine.connect() as conn:
            return (await conn.execute(SQL)).mappings().all()
    finally:
        await engine.dispose()


def check_chain(rows: list) -> str | None:
    """同租户内逐条验 prev_hash；返回失败描述（None = 通过）。"""

    prev = ""
    for row in rows:
        if row["prev_hash"] != prev:
            return (
                f"chain_seq={row['chain_seq']} prev_hash 不匹配："
                f"期望 {prev[:12]}…，实际 {(row['prev_hash'] or '')[:12]}…"
            )
        prev = row["hash"]
    return None


def render_extra(row) -> str:
    created = row["created_at"].isoformat() if row["created_at"] else "-"
    return (
        f"chain_seq={row['chain_seq']} {row['object_type']} "
        f"object_id={row['object_id']} created_at={created}"
    )


async def verify_all(dsn: str, scenarios: list[str], seed_exact: bool) -> int:
    seeds = {name: seed_tenant(name) for name in scenarios}
    rows = await fetch_rows(dsn)

    problems = 0
    seqs = [row["chain_seq"] for row in rows]
    duplicates = sorted({seq for seq in seqs if seqs.count(seq) > 1})
    if duplicates:
        print(f"[FAIL] chain_seq 全局不唯一：{duplicates}")
        problems += 1

    by_tenant: dict[UUID, list] = defaultdict(list)
    for row in rows:
        by_tenant[row["tenant_id"]].append(row)

    seed_total = 0
    runtime_total = 0
    checked_total = 0

    for scenario, (tenant_id, expected, seed_tail) in seeds.items():
        own = sorted(by_tenant.get(tenant_id, []), key=lambda row: row["chain_seq"])
        seed_total += expected
        checked_total += len(own)

        broken = check_chain(own)
        if broken:
            print(f"[FAIL] {scenario} tenant={tenant_id} 链断：{broken}")
            problems += 1
            continue

        if len(own) < expected:
            print(
                f"[FAIL] {scenario} tenant={tenant_id} "
                f"checked={len(own)}/{expected}（漏灌：种子 {expected} 条，库里只有 {len(own)} 条）"
            )
            problems += 1
            continue

        prefix_tail = own[expected - 1]["hash"]
        if prefix_tail != seed_tail:
            print(
                f"[FAIL] {scenario} tenant={tenant_id} "
                f"种子前缀尾不符：前 {expected} 条尾={prefix_tail[:12]}…，"
                f"种子={seed_tail[:12]}…"
            )
            problems += 1
            continue

        extra = own[expected:]
        runtime_total += len(extra)
        print(
            f"[ok] {scenario} tenant={tenant_id} checked={len(own)}/{expected} "
            f"runtime_added={len(extra)}"
        )
        for row in extra:
            print(f"       + 运行时新增 {render_extra(row)}")
        if seed_exact and extra:
            print(
                f"[FAIL] {scenario} tenant={tenant_id} "
                f"--seed-exact 下出现 {len(extra)} 条运行时新增"
            )
            problems += 1

    seeded = {tenant for tenant, _, _ in seeds.values()}
    for tenant_id, own in sorted(by_tenant.items()):
        if tenant_id in seeded:
            continue
        own = sorted(own, key=lambda row: row["chain_seq"])
        broken = check_chain(own)
        if broken:
            print(f"[FAIL] 非种子租户 tenant={tenant_id} 链断：{broken}")
            problems += 1
        else:
            print(f"[ok] 非种子租户 tenant={tenant_id} checked={len(own)}（仅链自洽）")

    mode = "seed-exact" if seed_exact else "prefix"
    print(
        f"TOTAL seed_images={seed_total} runtime_added={runtime_total} "
        f"images={len(rows)} mode={mode} problems={problems}"
    )
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 PostgreSQL 读回审计影像，复验哈希链与种子前缀"
    )
    parser.add_argument("--scenario", action="append", help="场景目录名，可重复")
    parser.add_argument("--all", action="store_true", help="校验全部场景（默认）")
    parser.add_argument(
        "--seed-exact",
        action="store_true",
        help="严格档：出现任何运行时新增即失败（只用于空库 / CI）",
    )
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
    return asyncio.run(verify_all(dsn, scenarios, args.seed_exact))


if __name__ == "__main__":
    raise SystemExit(main())
