#!/usr/bin/env python3
"""幂等加载 smart-ERP demo 数据集。

用法（Python 3.12）：
    python demo/load.py --scenario manufacturing-multi-plant --dsn postgresql://...
    python demo/load.py --all --dsn postgresql://...
    python demo/load.py --scenario fmcg-distribution --dry-run

幂等语义：
- demo.* 业务表：先按 scenario 删除，再整批插入（同一份数据重复执行结果一致）；
- erp.* 投影：先按 tenant_id 删除本场景的行，再插入；删除时临时关闭用户触发器
  （erp.recon_* / ticket_event / audit_image / approval_record 是 append-only，
  只有表属主可以 `ALTER TABLE ... DISABLE TRIGGER USER`，请用迁移账号/属主执行）；
- 任一场景失败即整体回滚，不会留下半套数据。

依赖白名单：asyncpg（Apache-2.0）。不引入 psycopg（LGPL）。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None

ROOT = Path(__file__).resolve().parent
SCEN_DIR = ROOT / "scenarios"
SCHEMA_FILE = ROOT / "schema.sql"

DEMO_TABLES = (
    "scenario", "organization", "warehouse", "currency", "tax_rate",
    "customer", "supplier", "item", "bom_line",
    "sales_order", "sales_order_line", "sales_delivery", "sales_delivery_line",
    "sales_invoice", "sales_invoice_line",
    "purchase_order", "purchase_order_line", "goods_receipt", "goods_receipt_line",
    "purchase_invoice", "purchase_invoice_line", "inventory_lot",
    "production_order", "production_material_issue", "gl_entry",
    "recon_case", "ticket_case",
    "promotion", "rebate_accrual", "fx_rate", "customs_declaration", "intercompany_settlement",
)

ERP_INSERT_ORDER = (
    ("metering_price_book", "price_book"),
    ("recon_batch", "recon_batches"),
    ("recon_line", "recon_lines"),
    ("ticket", "tickets"),
    ("ticket_step", "ticket_steps"),
    ("ticket_event", "ticket_events"),
    ("audit_image", "audit_images"),
    ("approval_record", "approval_records"),
    ("recon_line_link", "recon_line_links"),
)

ERP_PURGE_ORDER = (
    "recon_line_link", "recon_line_event", "recon_line", "recon_batch",
    "audit_image", "approval_record", "ticket_event", "ticket_step", "ticket",
    "metering_price_book",
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def read_manifest(scenario: str) -> dict:
    path = SCEN_DIR / scenario / "manifest.json"
    if not path.exists():
        fail(f"场景 {scenario} 未生成，请先运行 demo/tools/build_datasets.py")
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def parse_value(value, data_type: str, udt_name: str, nullable: bool):
    if value is None or value == "":
        if isinstance(value, str) and data_type in ("text", "character varying", "character"):
            return "" if not nullable else None
        if value == "" and data_type in ("text", "character varying", "character"):
            return "" if not nullable else None
        return None
    if isinstance(value, bool):
        return value
    if data_type in ("numeric", "real", "double precision"):
        return Decimal(str(value))
    if data_type in ("integer", "smallint", "bigint"):
        return int(value)
    if data_type == "boolean":
        return str(value).lower() in ("true", "1", "t", "yes")
    if data_type == "date":
        return date.fromisoformat(str(value))
    if data_type.startswith("timestamp"):
        return datetime.fromisoformat(str(value))
    if data_type == "uuid":
        return UUID(str(value))
    if data_type == "jsonb" or data_type == "json":
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if data_type == "ARRAY":
        if isinstance(value, list):
            return value
        return [part for part in str(value).split(",") if part]
    return value


def encode_for_jsonb(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


async def column_meta(conn: asyncpg.Connection, schema: str, table: str) -> dict[str, dict]:
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, udt_name, is_nullable
          FROM information_schema.columns
         WHERE table_schema = $1 AND table_name = $2
         ORDER BY ordinal_position
        """,
        schema,
        table,
    )
    return {
        row["column_name"]: {
            "data_type": row["data_type"],
            "udt_name": row["udt_name"],
            "nullable": row["is_nullable"] == "YES",
        }
        for row in rows
    }


async def insert_rows(
    conn: asyncpg.Connection,
    schema: str,
    table: str,
    rows: list[dict],
    columns: list[str] | None = None,
) -> int:
    if not rows:
        return 0
    meta = await column_meta(conn, schema, table)
    columns = columns or [key for key in rows[0] if key in meta]
    unknown = [key for key in columns if key not in meta]
    if unknown:
        fail(f"{schema}.{table} 不存在列 {unknown}（契约/schema 漂移）")
    placeholders = ", ".join(f"${idx + 1}" for idx in range(len(columns)))
    statement = f'INSERT INTO {schema}.{table} ({", ".join(columns)}) VALUES ({placeholders})'
    payload = []
    for row in rows:
        values = []
        for key in columns:
            info = meta[key]
            raw = row.get(key)
            if info["data_type"] in ("jsonb", "json"):
                raw = encode_for_jsonb(raw)
                values.append(raw)
                continue
            values.append(parse_value(raw, info["data_type"], info["udt_name"], info["nullable"]))
        payload.append(values)
    await conn.executemany(statement, payload)
    return len(payload)


async def purge_demo(conn: asyncpg.Connection, scenario: str) -> None:
    for table in reversed(DEMO_TABLES):
        await conn.execute(f"DELETE FROM demo.{table} WHERE scenario = $1", scenario)


async def purge_erp(conn: asyncpg.Connection, tenant: UUID) -> None:
    # 关闭用户触发器（append-only / hash 链）后按依赖顺序删除；ALTER 是事务性的，
    # 任一步失败会随事务回滚，触发器状态不会残留。
    for table in ERP_PURGE_ORDER:
        try:
            await conn.execute(f"ALTER TABLE erp.{table} DISABLE TRIGGER USER")
        except asyncpg.PostgresError as exc:
            fail(
                f"无法关闭 erp.{table} 的用户触发器（需要表属主权限）：{exc}\n"
                "请用迁移账号/表属主执行加载；云端导入请用 pg_restore 到空库。"
            )
    await conn.execute(
        "DELETE FROM erp.ticket_step WHERE ticket_id IN (SELECT id FROM erp.ticket WHERE tenant_id = $1)",
        tenant,
    )
    for table in ERP_PURGE_ORDER:
        if table == "ticket_step":
            continue
        await conn.execute(f"DELETE FROM erp.{table} WHERE tenant_id = $1", tenant)
    for table in reversed(ERP_PURGE_ORDER):
        await conn.execute(f"ALTER TABLE erp.{table} ENABLE TRIGGER USER")


async def load_scenario(conn: asyncpg.Connection, scenario: str, *, with_erp: bool) -> dict:
    manifest = read_manifest(scenario)
    if not manifest["checks"]["ok"]:
        fail(f"场景 {scenario} 自检未通过：{manifest['checks']['problems']}")
    folder = SCEN_DIR / scenario
    counts: dict[str, int] = {}
    async with conn.transaction():
        await purge_demo(conn, scenario)
        for table in DEMO_TABLES:
            path = folder / f"{table}.csv"
            if not path.exists():
                counts[table] = 0
                continue
            _, rows = read_csv(path)
            counts[table] = await insert_rows(conn, "demo", table, rows)

        erp_counts: dict[str, int] = {}
        if with_erp:
            seed = json.loads((folder / "erp_seed.json").read_text(encoding="utf-8"))
            tenant = UUID(seed["tenant_id"])
            await purge_erp(conn, tenant)
            for table, key in ERP_INSERT_ORDER:
                erp_counts[table] = await insert_rows(conn, "erp", table, seed.get(key, []))
        else:
            erp_counts = {table: 0 for table, _ in ERP_INSERT_ORDER}

        dataset_sha = hashlib.sha256(
            json.dumps(manifest["files"], sort_keys=True).encode("utf-8")
        ).hexdigest()
        await conn.execute(
            """
            INSERT INTO demo.load_log (scenario, dataset_sha256, row_counts, erp_counts, loaded_by)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5)
            """,
            scenario,
            dataset_sha,
            json.dumps(counts, ensure_ascii=False),
            json.dumps(erp_counts, ensure_ascii=False),
            os.environ.get("USERNAME") or os.environ.get("USER") or "demo-loader",
        )
    return {"scenario": scenario, "demo": counts, "erp": erp_counts, "sha256": dataset_sha}


def dry_run(scenario: str) -> dict:
    manifest = read_manifest(scenario)
    folder = SCEN_DIR / scenario
    counts: dict[str, int] = {}
    for table in DEMO_TABLES:
        path = folder / f"{table}.csv"
        counts[table] = len(read_csv(path)[1]) if path.exists() else 0
    seed_path = folder / "erp_seed.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8")) if seed_path.exists() else {}
    erp_counts = {table: len(seed.get(key, [])) for table, key in ERP_INSERT_ORDER}
    print(f"[dry-run] {scenario}: 自检 {'通过' if manifest['checks']['ok'] else '失败'}")
    for table, count in counts.items():
        if count:
            print(f"  demo.{table:<28} {count:>7}")
    for table, count in erp_counts.items():
        if count:
            print(f"  erp.{table:<29} {count:>7}")
    if manifest["checks"]["problems"]:
        for problem in manifest["checks"]["problems"]:
            print(f"  ! {problem}")
    return {"scenario": scenario, "demo": counts, "erp": erp_counts}


async def async_main(args: argparse.Namespace) -> int:
    scenarios = args.scenario or [p.name for p in SCEN_DIR.iterdir() if (p / "manifest.json").exists()]
    if not scenarios:
        fail("demo/scenarios 下没有可用场景，请先运行生成器")
    if args.dry_run:
        failed = False
        for scenario in scenarios:
            result = dry_run(scenario)
            failed = failed or not read_manifest(scenario)["checks"]["ok"]
        return 1 if failed else 0
    if asyncpg is None:
        fail("缺少 asyncpg（Apache-2.0）：请用项目 .venv 或 pip install asyncpg")
    dsn = args.dsn or os.environ.get("DEMO_DSN")
    if not dsn:
        fail("必须提供 --dsn 或环境变量 DEMO_DSN（示例：postgresql://user:pass@host:5432/smart_erp）")
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        results = []
        for scenario in scenarios:
            result = await load_scenario(conn, scenario, with_erp=not args.no_erp)
            results.append(result)
            demo_total = sum(result["demo"].values())
            erp_total = sum(result["erp"].values())
            print(f"[loaded] {scenario}: demo {demo_total} 行，erp {erp_total} 行")
            top = sorted(result["demo"].items(), key=lambda item: item[1], reverse=True)[:8]
            print("          " + "，".join(f"{table} {count}" for table, count in top))
            print("          erp: " + "，".join(f"{table} {count}" for table, count in result["erp"].items()))
        print("\n[done] 重复执行同一命令即为幂等重灌。")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="加载 smart-ERP demo 数据集（幂等）")
    parser.add_argument("--scenario", action="append", help="场景目录名，可重复")
    parser.add_argument("--all", action="store_true", help="加载全部场景（默认）")
    parser.add_argument("--dsn", help="PostgreSQL DSN；也可用 DEMO_DSN")
    parser.add_argument("--dry-run", action="store_true", help="只统计与自检，不连库")
    parser.add_argument("--no-erp", action="store_true", help="只灌 demo 业务表，不写 erp.* 投影")
    args = parser.parse_args()
    if not args.all and not args.scenario:
        args.all = True
    if args.all:
        args.scenario = None
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
