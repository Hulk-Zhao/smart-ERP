"""T704 对账性能基线脚本（可复现，非估算）。

口径（务必连同数字一起引用，缺一即失真）
------------------------------------------------
测的是 ``apps.api.service.run_recon`` 的服务端一段：
    请求体校验（JSON→Pydantic）→ 引擎（``build_request`` + ``reconcile``，
    含重复计费识别、三层匹配、归因）→ 落库（``SqlReconStore.save``，
    批次 1 行 + 每条对账行 1 行 INSERT）
    → ``list_recon_diffs`` 首页 50 条（含 ``result_view`` 反序列化）。

**不含**：网络传输、文件上传/解压、连接器拉数、前端渲染、鉴权与四道闸。
因此对外只能说「这一段服务端耗时」，不能说「系统处理 N 行需要 X 秒」。

为什么在进程内调用而不是打 HTTP：当前 api 进程没有装配连接器时，
``POST /v1/recon/runs`` 会在 ``pick_meta`` 处 503 ``E-CONN-UNAVAILABLE``，
HTTP 端到端产不出成功批次。
本脚本绕过 ``pick_meta`` 与 ``_authorize``（两者不参与计时），
校验、引擎、store、真实 PG 全部是生产代码。

环境红线
--------
- 只允许指向**独立压测库**：必须显式给 ``--dsn`` 或 ``ERP_TEST_DATABASE_URL``，
  且必须带 ``--yes``。
- 禁止在 live 演示栈的 PG 上跑；禁止为此重启/重灌演示栈。
- 数据集为合成数据：数量/金额分布取自
  ``demo/scenarios/manufacturing-multi-plant/recon_case.csv``；
  每行 ``(supplier_code, item_code)`` 唯一（200 供应商 × 最多 500 物料），
  我方与供应商 1:1，
  每 100 行注入 1 行数量差异（×1.02）。**不含**同供应商同物料多单
  （会制造候选笛卡尔积，属另一问题）。

用法（在 app 镜像容器内执行，镜像已含运行期依赖）
--------------------------------------------------
    docker cp demo/scenarios/manufacturing-multi-plant/recon_case.csv \
        s7b-api-mfg:/tmp/scen/recon_case.csv
    docker cp tests/perf/recon_baseline.py s7b-api-mfg:/tmp/perf.py
    docker exec s7b-api-mfg python -u /tmp/perf.py \
        --dsn postgresql+asyncpg://postgres:***@s7b-pg:5432/smart_erp \
        --scenario-csv /tmp/scen/recon_case.csv \
        --rows 10000 50000 100000 --tag baseline --yes

输出：每个档位一行 JSON（含硬件/PG 版本/timers），最后一行 ``summary``。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

TENANT = UUID("a927f664-796d-5a6c-bb43-c052cd4a0e2e")
USER = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
SUPPLIER_COUNT = 200
ITEM_COUNT_MAX = 500
DEFAULT_SCENARIO = "demo/scenarios/manufacturing-multi-plant/recon_case.csv"


def _num(v: str) -> str | None:
    v = (v or "").strip()
    return v or None


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mem_total_kib() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def _rss_kib() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def environment() -> dict:
    """硬件/运行时信息，随每一档一起输出，避免数字脱离环境被转述。"""
    return {
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "mem_total_mib": round(_mem_total_kib() / 1024) if _mem_total_kib() else None,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "rss_mib_end": round(_rss_kib() / 1024) if _rss_kib() else None,
    }


def build_raw(n: int, samples: list[tuple[Decimal, Decimal]], tag: str = "") -> dict:
    """按 demo 的 qty/amount 分布生成 N 条我方 + N 条供应商行，键唯一。"""
    suppliers = [f"S-{i:03d}" for i in range(SUPPLIER_COUNT)]
    item_count = min(ITEM_COUNT_MAX, max(1, -(-n // SUPPLIER_COUNT)))
    items = [f"I-{i:04d}" for i in range(item_count)]
    suffix = f"-{tag}" if tag else ""

    our, sup = [], []
    for i in range(n):
        qty, amount = samples[i % len(samples)]
        supplier = suppliers[i % SUPPLIER_COUNT]
        item = items[(i // SUPPLIER_COUNT) % item_count]
        our.append({
            "system": "u8",
            "doc_no": f"GR-{i:07d}{suffix}",
            "line_no": 1,
            "supplier_code": supplier,
            "item_code": item,
            "unit": "PCS",
            "qty": str(qty),
            "amount": str(amount),
            "currency": "CNY",
            "period": "2026-09",
            "coord": {"i": i},
        })
        sup_qty = qty * Decimal("1.02") if i % 100 == 0 else qty
        sup.append({
            "system": "statement",
            "doc_no": f"ST-{i:07d}{suffix}",
            "line_no": 1,
            "supplier_code": supplier,
            "item_code": item,
            "unit": "PCS",
            "qty": str(sup_qty),
            "amount": str(amount),
            "currency": "CNY",
            "period": "2026-09",
            "coord": {"i": i},
        })

    return {
        "scope": {
            "org_id": "ORG-SZ",
            "period": "2026-09",
            "system_ids": ["u8", "wms", "sap"],
        },
        "suppliers": suppliers,
        "sources": {"erp": True},
        "dataset": {
            "our_lines": our,
            "supplier_lines": sup,
            "master_data": {
                "suppliers": [{"code": s, "name": s, "aliases": []} for s in suppliers],
                "items": [{"code": it, "name": it, "base_unit": "PCS"} for it in items],
                "unit_conversions": [],
            },
        },
    }


def load_samples(path: Path) -> list[tuple[Decimal, Decimal]]:
    rows = [
        (Decimal(r["qty_mine"]), Decimal(r["our_amount"]))
        for r in csv.DictReader(path.open(encoding="utf-8", newline=""))
        if (r["qty_mine"] or "").strip() and (r["our_amount"] or "").strip()
    ]
    if not rows:
        raise SystemExit(f"{path} 无可用样本（qty_mine / our_amount 均为空）")
    return rows


async def _pg_version(session_factory) -> str | None:
    from sqlalchemy import text

    async with session_factory() as session:
        return (await session.execute(text("select version()"))).scalar_one()


async def run_tier(rows: int, samples: list[tuple[Decimal, Decimal]], tag: str) -> dict:
    from apps.api import service
    from apps.api.container import get_container
    from apps.api.recon_store import SqlReconStore
    from apps.api.schemas import ReconRunIn
    from apps.security.identity import SessionToken
    from apps.security.scope import ScopeGrant

    raw = build_raw(rows, samples, tag)
    t0 = time.perf_counter()
    body = ReconRunIn.model_validate(raw)
    t_validate = time.perf_counter() - t0

    timers = {
        "engine_parse": 0.0,
        "engine_reconcile": 0.0,
        "persist": 0.0,
        "view_build": 0.0,
        "snapshot_build": 0.0,
        "price": 0.0,
    }
    orig = {
        "build": service.build_request,
        "reconcile": service.reconcile,
        "save": SqlReconStore.save,
        "view": service._batch_view,
        "snapshot": service.snapshot_from_batch,
        "price": service._price_snapshot,
    }

    def timed_sync(key, fn):
        def wrapper(*a, **k):
            start = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                timers[key] += time.perf_counter() - start

        return wrapper

    async def save_timed(self, snapshot):
        start = time.perf_counter()
        try:
            return await orig["save"](self, snapshot)
        finally:
            timers["persist"] += time.perf_counter() - start

    async def price_timed(*a, **k):
        start = time.perf_counter()
        try:
            return await orig["price"](*a, **k)
        finally:
            timers["price"] += time.perf_counter() - start

    service.build_request = timed_sync("engine_parse", orig["build"])
    service.reconcile = timed_sync("engine_reconcile", orig["reconcile"])
    service._batch_view = timed_sync("view_build", orig["view"])
    service.snapshot_from_batch = timed_sync("snapshot_build", orig["snapshot"])
    service._price_snapshot = price_timed
    SqlReconStore.save = save_timed

    container = get_container()
    # 只绕过装配断言与鉴权：两者都不在「对账服务端耗时」口径内。
    container.pick_meta = lambda ids: SimpleNamespace(system="u8")
    service._authorize = lambda *a, **k: None

    now = datetime.now(UTC)
    session = SessionToken(
        token_id="perf",
        tenant_id=TENANT,
        user_id=USER,
        roles=frozenset({"reviewer"}),
        grant=ScopeGrant(
            org_ids=frozenset({"ORG-SZ"}),
            periods=frozenset({"2026-09"}),
            system_ids=frozenset({"u8", "wms", "sap"}),
        ),
        issued_at=now,
        expires_at=now + timedelta(hours=8),
    )

    t1 = time.perf_counter()
    out = await service.run_recon(container, session, body=body)
    t_run = time.perf_counter() - t1

    t2 = time.perf_counter()
    diffs, _ = await service.list_recon_diffs(
        container,
        session,
        scope_in=body.scope,
        batch_id=out.batch_id,
        diff_type=None,
        needs_manual=None,
        supplier_code=None,
        billing_bucket=None,
        cursor=None,
        limit=50,
    )
    t_read = time.perf_counter() - t2

    return {
        "rows_each_side": rows,
        "total_line_objects": rows * 2,
        "batch_id": str(out.batch_id),
        "status": out.status,
        "diff_count": len(diffs),
        "validate_s": round(t_validate, 3),
        "engine_parse_s": round(timers["engine_parse"], 3),
        "engine_reconcile_s": round(timers["engine_reconcile"], 3),
        "engine_total_s": round(timers["engine_parse"] + timers["engine_reconcile"], 3),
        "view_build_s": round(timers["view_build"], 3),
        "snapshot_build_s": round(timers["snapshot_build"], 3),
        "price_s": round(timers["price"], 3),
        "persist_s": round(timers["persist"], 3),
        "run_recon_total_s": round(t_run, 3),
        "read_first_page_s": round(t_read, 3),
        "server_total_s": round(t_run + t_read, 3),
        "rss_mib_end": round(_rss_kib() / 1024) if _rss_kib() else None,
    }


async def main_async(args) -> int:
    dsn = args.dsn or os.environ.get("ERP_TEST_DATABASE_URL")
    if not dsn:
        print(
            "必须显式提供 --dsn 或 ERP_TEST_DATABASE_URL（禁止默认连 live 库）",
            file=sys.stderr,
        )
        return 2
    if not args.yes:
        print(
            f"将向 {dsn.split('@')[-1]} 写入对账批次；确认独立压测库后加 --yes",
            file=sys.stderr,
        )
        return 2

    os.environ["ERP_DATABASE_URL"] = dsn
    scenario = Path(args.scenario_csv or DEFAULT_SCENARIO)
    samples = load_samples(scenario)

    from apps.platform.db.session import build_engine, build_session_factory

    session_factory = build_session_factory(build_engine())

    meta = {
        "script": "tests/perf/recon_baseline.py",
        "measured": (
            "validate+engine+persist+first_page；"
            "不含网络/上传/连接器拉数/鉴权"
        ),
        "data": (
            f"synthetic from {scenario}；每行 (supplier,item) 唯一；"
            "每 100 行 1 行数量差异"
        ),
        "processes": "单进程单 worker、引擎单线程",
        "db": dsn.split("@")[-1],
        "db_version": None,
        **environment(),
    }
    try:
        meta["db_version"] = await _pg_version(session_factory)
    except Exception as exc:  # noqa: BLE001 - 版本采集失败不应中断压测
        meta["db_version"] = f"unavailable: {exc.__class__.__name__}"

    results = []
    for rows in args.rows:
        row = await run_tier(rows, samples, args.tag)
        row["environment"] = environment()
        row["db_version"] = meta["db_version"]
        print(json.dumps(row, ensure_ascii=False), flush=True)
        results.append(row)

    print(
        json.dumps({"summary": True, "meta": meta, "tiers": results}, ensure_ascii=False),
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="T704 对账性能基线（服务端口径）")
    parser.add_argument("--rows", type=int, nargs="+", default=[10000, 50000, 100000])
    parser.add_argument(
        "--tag", default="baseline", help="附加到单号，避免与历史批次幂等冲突"
    )
    parser.add_argument(
        "--dsn", default=None, help="独立压测库 DSN；也可用 ERP_TEST_DATABASE_URL"
    )
    parser.add_argument(
        "--scenario-csv", default=None, help=f"demo 分布样本，默认 {DEFAULT_SCENARIO}"
    )
    parser.add_argument(
        "--yes", action="store_true", help="确认目标是独立压测库，允许写入"
    )
    args = parser.parse_args()

    import asyncio

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
