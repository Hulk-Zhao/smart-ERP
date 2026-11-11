#!/usr/bin/env python3
"""把已加载的 demo 数据一键导出为单个可传输的 pg_dump 自定义格式文件。

用法（Python 3.12）：
    # 本机没有 pg_dump 时，借已运行的 PG 容器导出（推荐）
    python demo/export.py --dsn postgresql://postgres:postgres@localhost:55441/smart_erp_demo \
        --docker-container s8-launch-pg --out demo/dist --verify
    # 本机有 pg_dump 时直接导出
    python demo/export.py --dsn postgresql://... --pg-dump pg_dump --out demo/dist

产物：
    demo/dist/<name>.dump             单文件，含 demo + erp 两个 schema 的结构与数据
    demo/dist/<name>.manifest.json    版本、sha256、行数摘要、导入命令
    demo/dist/SHA256SUMS              校验和

导入（云服务器，一条命令）：
    PGDATABASE=smart_erp PGHOST=... PGUSER=... bash demo/cloud/import.sh <name>.dump

依赖：仅标准库 + 系统 docker/pg_dump/pg_restore；不引入第三方包。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "dist"
SCEN_INDEX = ROOT / "scenarios" / "index.json"

# 导出/校验覆盖的表：demo 全量 + erp 投影（与 load.py 的加载范围一致）
VERIFY_TABLES = (
    "demo.scenario", "demo.organization", "demo.warehouse", "demo.currency",
    "demo.tax_rate", "demo.customer", "demo.supplier", "demo.item", "demo.bom_line",
    "demo.sales_order", "demo.sales_order_line", "demo.sales_delivery",
    "demo.sales_delivery_line", "demo.sales_invoice", "demo.sales_invoice_line",
    "demo.purchase_order", "demo.purchase_order_line", "demo.goods_receipt",
    "demo.goods_receipt_line", "demo.purchase_invoice", "demo.purchase_invoice_line",
    "demo.inventory_lot", "demo.production_order", "demo.production_material_issue",
    "demo.gl_entry", "demo.recon_case", "demo.ticket_case", "demo.promotion",
    "demo.rebate_accrual", "demo.fx_rate", "demo.customs_declaration",
    "demo.intercompany_settlement",
    "erp.metering_price_book", "erp.recon_batch", "erp.recon_line", "erp.ticket",
    "erp.ticket_step", "erp.ticket_event", "erp.audit_image", "erp.approval_record",
    "erp.recon_line_link",
)

CST = timezone(timedelta(hours=8))


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def parse_dsn(dsn: str) -> dict:
    parsed = urlparse(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    if parsed.scheme not in ("postgresql", "postgres"):
        fail(f"无法识别的 DSN：{dsn}")
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        fail("DSN 缺少数据库名")
    return {
        "user": unquote(parsed.username or "postgres"),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "database": database,
    }


def run(cmd: list[str], *, env: dict | None = None, stream: bool = False) -> str:
    merged = {**os.environ, **(env or {})}
    if stream:
        proc = subprocess.run(cmd, env=merged)
        if proc.returncode != 0:
            fail(f"命令失败（{proc.returncode}）：{shlex.join(cmd)}")
        return ""
    proc = subprocess.run(cmd, env=merged, capture_output=True, text=True)
    if proc.returncode != 0:
        fail(
            f"命令失败（{proc.returncode}）：{shlex.join(cmd)}\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    return proc.stdout.strip()


class Pg:
    """本机 pg_dump/pg_restore，或通过 docker exec 借容器内的客户端。"""

    def __init__(self, dsn: str, *, docker_container: str | None, pg_dump: str | None):
        self.dsn = dsn
        self.info = parse_dsn(dsn)
        self.container = docker_container
        self.pg_dump = pg_dump or "pg_dump"
        self.pg_restore = "pg_restore"
        self.psql = "psql"

    # --- 底层执行 ---------------------------------------------------------
    def _exec(self, argv: list[str], *, dbname: str | None = None) -> str:
        if self.container:
            cmd = ["docker", "exec", "-e", f"PGPASSWORD={self.info['password']}", self.container, *argv]
            return run(cmd)
        env = {"PGPASSWORD": self.info["password"]}
        return run(argv, env=env)

    def server_version(self) -> str:
        if self.container:
            return run(["docker", "exec", self.container, "pg_dump", "--version"])
        return run([self.pg_dump, "--version"])

    # --- 导出 -------------------------------------------------------------
    def dump(self, target: Path, schemas: tuple[str, ...]) -> None:
        schema_args = [arg for schema in schemas for arg in ("-n", schema)]
        if self.container:
            tmp = f"/tmp/{target.name}"
            argv = ["pg_dump", "-U", self.info["user"], "-Fc", "--no-owner", "--no-privileges",
                    *schema_args, "-d", self.info["database"], "-f", tmp]
            self._exec(argv)
            run(["docker", "cp", f"{self.container}:{tmp}", str(target)])
            run(["docker", "exec", self.container, "rm", "-f", tmp])
            return
        env = {"PGPASSWORD": self.info["password"]}
        argv = [self.pg_dump, "-h", self.info["host"], "-p", str(self.info["port"]),
                "-U", self.info["user"], "-Fc", "--no-owner", "--no-privileges",
                *schema_args, "-d", self.info["database"], "-f", str(target)]
        run(argv, env=env)

    # --- 还原/查询 --------------------------------------------------------
    def restore(self, dump_file: Path, database: str) -> None:
        if self.container:
            tmp = f"/tmp/{dump_file.name}.restore"
            run(["docker", "cp", str(dump_file), f"{self.container}:{tmp}"])
            argv = ["pg_restore", "-U", self.info["user"], "--no-owner", "--no-privileges",
                    "-d", database, tmp]
            self._exec(argv)
            run(["docker", "exec", self.container, "rm", "-f", tmp])
            return
        env = {"PGPASSWORD": self.info["password"]}
        run([self.pg_restore, "-h", self.info["host"], "-p", str(self.info["port"]),
             "-U", self.info["user"], "--no-owner", "--no-privileges", "-d", database, str(dump_file)],
            env=env)

    def scalar(self, database: str, sql: str) -> str:
        if self.container:
            return self._exec(["psql", "-U", self.info["user"], "-tA", "-d", database, "-c", sql])
        env = {"PGPASSWORD": self.info["password"]}
        return run([self.psql, "-h", self.info["host"], "-p", str(self.info["port"]),
                    "-U", self.info["user"], "-tA", "-d", database, "-c", sql], env=env)

    def admin(self, sql: str) -> None:
        self.scalar("postgres", sql)

    def counts(self, database: str) -> dict[str, int]:
        parts = [
            f"SELECT '{table}', count(*) FROM {table}"
            for table in VERIFY_TABLES
        ]
        sql = " UNION ALL ".join(parts) + " ORDER BY 1"
        output = self.scalar(database, sql)
        counts: dict[str, int] = {}
        for line in output.splitlines():
            if "|" in line:
                table, value = line.split("|", 1)
                counts[table] = int(value)
        return counts


def scenario_summary() -> list[dict]:
    if not SCEN_INDEX.exists():
        return []
    index = json.loads(SCEN_INDEX.read_text(encoding="utf-8"))
    summary = []
    for item in index.get("scenarios", []):
        rows = item.get("row_counts", {})
        summary.append({
            "scenario": item["scenario"],
            "display_name": item.get("display_name"),
            "demo_rows": sum(rows.values()),
            "sales_order_lines": rows.get("sales_order_line", 0),
            "customers": rows.get("customer", 0),
            "suppliers": rows.get("supplier", 0),
            "items": rows.get("item", 0),
        })
    return summary


def verify(pg: Pg, dump_file: Path, source_db: str, keep: bool) -> dict:
    scratch = f"{source_db}_verify"
    print(f"[verify] 还原到临时库 {scratch} ...")
    pg.admin(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
    pg.admin(f'CREATE DATABASE "{scratch}" WITH TEMPLATE template0 ENCODING \'UTF8\'')
    try:
        pg.restore(dump_file, scratch)
        source_counts = pg.counts(source_db)
        restored_counts = pg.counts(scratch)
        mismatches = {
            table: {"source": source_counts.get(table), "restored": restored_counts.get(table)}
            for table in VERIFY_TABLES
            if source_counts.get(table) != restored_counts.get(table)
        }
        status = "passed" if not mismatches else "failed"
        print(f"[verify] {status}：{len(VERIFY_TABLES)} 张表，差异 {len(mismatches)} 处")
        for table, delta in mismatches.items():
            print(f"  ! {table}: {delta['source']} -> {delta['restored']}")
        return {
            "status": status,
            "checked_tables": len(VERIFY_TABLES),
            "mismatches": mismatches,
            "scratch_database": scratch if keep else None,
        }
    finally:
        if not keep:
            pg.admin(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 demo 数据为单个 pg_dump 自定义格式文件")
    parser.add_argument("--dsn", help="源数据库 DSN；也可用 DEMO_DSN")
    parser.add_argument("--docker-container", help="借哪个容器的 pg_dump（本机无 pg_dump 时使用）")
    parser.add_argument("--pg-dump", help="本机 pg_dump 路径（缺省从 PATH 查找）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录（默认 demo/dist）")
    parser.add_argument("--name", help="产物名（默认 smart-erp-demo-<YYYYMMDD-HHMM>）")
    parser.add_argument("--verify", action="store_true", help="还原到临时库并逐表比对行数")
    parser.add_argument("--keep-verify-db", action="store_true", help="保留临时校验库，便于人工检查")
    parser.add_argument("--schema", action="append", help="要导出的 schema（默认 demo、erp）")
    args = parser.parse_args()

    dsn = args.dsn or os.environ.get("DEMO_DSN")
    if not dsn:
        fail("必须提供 --dsn 或环境变量 DEMO_DSN")
    if args.docker_container and args.pg_dump:
        fail("--docker-container 与 --pg-dump 只能二选一")
    if not args.docker_container and not args.pg_dump and not shutil.which("pg_dump"):
        fail("本机找不到 pg_dump：请用 --docker-container 借容器，或 --pg-dump 指定路径")

    name = args.name or f"smart-erp-demo-{datetime.now(CST).strftime('%Y%m%d-%H%M')}"
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    dump_file = out_dir / f"{name}.dump"
    schemas = tuple(args.schema or ("demo", "erp"))

    pg = Pg(dsn, docker_container=args.docker_container, pg_dump=args.pg_dump)
    server_version = pg.server_version()
    print(f"[export] 源库 {pg.info['database']} @ {pg.info['host']}:{pg.info['port']}")
    print(f"[export] {server_version}")

    print(f"[export] 导出 schema {', '.join(schemas)} -> {dump_file.name} ...")
    pg.dump(dump_file, schemas)
    if not dump_file.exists() or dump_file.stat().st_size == 0:
        fail("导出文件为空")

    sha = hashlib.sha256(dump_file.read_bytes()).hexdigest()
    size = dump_file.stat().st_size
    print(f"[export] 完成：{size} 字节，sha256={sha}")

    result: dict = {
        "artifact": dump_file.name,
        "sha256": sha,
        "bytes": size,
        "created_at": datetime.now(CST).isoformat(),
        "pg_dump_version": server_version,
        "source_database": pg.info["database"],
        "schemas": list(schemas),
        "scenarios": scenario_summary(),
        "import_command": (
            "PGDATABASE=smart_erp PGHOST=<pg-host> PGPORT=5432 PGUSER=erp_migrator "
            f"bash demo/cloud/import.sh {dump_file.name}"
        ),
    }

    if args.verify:
        result["verify"] = verify(pg, dump_file, pg.info["database"], args.keep_verify_db)

    manifest = out_dir / f"{name}.manifest.json"
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "SHA256SUMS").write_text(f"{sha}  {dump_file.name}\n", encoding="utf-8")

    print(f"[done] {dump_file}")
    print(f"[done] {manifest}")
    if args.verify:
        status = result["verify"]["status"]
        print(f"[done] 校验：{status}")
        return 0 if status == "passed" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
