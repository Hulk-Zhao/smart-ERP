"""主数据体检的性能基线（只读，不接 API、不写回）。

目标是用可复现的合成主数据回答一个问题：1000 / 1 万 / 10 万行混合主数据
（供应商 + 客户 + 物料 + 单据）跑完 24 条规则需要多久、峰值内存多少。

约束：
- 输入完全由 ``total_rows`` 决定，不使用随机数，同一参数两次生成的数据相等；
- 每个量级建议在**独立进程**里跑，避免上一个量级的峰值内存污染下一个；
- 只做 ``inspect`` 与报告序列化，不写数据库、不调用外部系统。

用法::

    python -m apps.masterdata.benchmark --rows 1000
    python -m apps.masterdata.benchmark --rows 100000 --json bench-100k.json

机器规格与实测结果见 ``PERFORMANCE.md``。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .engine import inspect
from .impact import build_downgrade_list
from .models import (
    CurrencyRate,
    Customer,
    Document,
    Item,
    Ledger,
    MasterDataSnapshot,
    Period,
    Supplier,
    TaxCode,
)

ORG_ID = "ORG-PERF"
LEDGER_ID = "L001"
PERIOD = "2026-10"
SOURCE = "benchmark://synthetic-mixed"

# 混合比例：物料最多、单据次之，供应商与客户占少数；合计严格等于 total_rows。
SUPPLIER_RATIO = Decimal("0.20")
CUSTOMER_RATIO = Decimal("0.15")
ITEM_RATIO = Decimal("0.35")


@dataclass(frozen=True)
class EntityCounts:
    suppliers: int
    customers: int
    items: int
    documents: int

    @property
    def total(self) -> int:
        return self.suppliers + self.customers + self.items + self.documents

    def to_dict(self) -> dict[str, int]:
        return {
            "suppliers": self.suppliers,
            "customers": self.customers,
            "items": self.items,
            "documents": self.documents,
            "total": self.total,
        }


@dataclass(frozen=True)
class BenchmarkResult:
    rows: int
    counts: EntityCounts
    build_seconds: float
    inspect_seconds: float
    serialize_seconds: float
    total_seconds: float
    findings: int
    baseline_working_set_bytes: int | None
    peak_working_set_bytes: int | None
    report_digest: str
    downgrade_digest: str

    def to_dict(self) -> dict[str, Any]:
        baseline = self.baseline_working_set_bytes
        peak = self.peak_working_set_bytes
        return {
            "rows": self.rows,
            "entities": self.counts.to_dict(),
            "build_seconds": round(self.build_seconds, 4),
            "inspect_seconds": round(self.inspect_seconds, 4),
            "serialize_seconds": round(self.serialize_seconds, 4),
            "total_seconds": round(self.total_seconds, 4),
            "findings": self.findings,
            "baseline_working_set_mb": (
                round(baseline / 1024 / 1024, 2) if baseline is not None else None
            ),
            "peak_working_set_mb": (
                round(peak / 1024 / 1024, 2) if peak is not None else None
            ),
            "peak_delta_mb": (
                round((peak - baseline) / 1024 / 1024, 2)
                if peak is not None and baseline is not None
                else None
            ),
            "report_digest": self.report_digest,
            "downgrade_digest": self.downgrade_digest,
            "python": sys.version.split()[0],
            "platform": f"{platform.system()} {platform.machine()}",
        }


def entity_counts(total_rows: int) -> EntityCounts:
    """按固定比例拆分总量；单据兜底保证四类都至少有 1 行。"""
    if total_rows < 4:
        raise ValueError(f"总行数至少为 4，收到 {total_rows}")
    suppliers = max(1, int(Decimal(total_rows) * SUPPLIER_RATIO))
    customers = max(1, int(Decimal(total_rows) * CUSTOMER_RATIO))
    items = max(1, int(Decimal(total_rows) * ITEM_RATIO))
    documents = total_rows - suppliers - customers - items
    if documents < 1:
        raise ValueError(f"总行数 {total_rows} 无法拆出单据行")
    return EntityCounts(suppliers, customers, items, documents)


def build_snapshot(total_rows: int) -> MasterDataSnapshot:
    """生成确定性的混合主数据快照，并按约 0.1%~0.5% 注入可预期缺陷。"""
    counts = entity_counts(total_rows)
    suppliers = tuple(
        Supplier(
            code=f"SUP-{index:07d}",
            name=f"基准供应商{index}",
            tax_no=f"9133PERF{index:010d}",
            bank_account=(
                None if index % 500 == 0 else f"6222{index:016d}"
            ),
            org_id=ORG_ID,
            ledger_id=LEDGER_ID,
            currency="CNY",
            payment_term="NET30",
            tax_code="VAT99" if index % 1000 == 0 else "VAT13",
        )
        for index in range(counts.suppliers)
    )
    customers = tuple(
        Customer(
            code=f"CUS-{index:07d}",
            name=f"基准客户{index}",
            tax_no=None if index % 400 == 0 else f"9133CUSP{index:010d}",
            org_id=ORG_ID,
            ledger_id=LEDGER_ID,
            currency="CNY",
            payment_term="NET30",
            tax_code="VAT13",
        )
        for index in range(counts.customers)
    )
    items = tuple(
        Item(
            code=f"IT-{index:07d}",
            name=f"基准物料{index}",
            base_unit=None if index % 300 == 0 else "PCS",
            category=f"C{index % 20:02d}",
            org_id=ORG_ID,
            ledger_id=LEDGER_ID,
            tax_code="VAT13",
        )
        for index in range(counts.items)
    )
    documents = tuple(
        Document(
            doc_no=f"DOC-{index:08d}",
            doc_type="采购订单",
            doc_period=PERIOD,
            posting_period="2026-09" if index % 2000 == 0 else PERIOD,
            org_id=ORG_ID,
            ledger_id=LEDGER_ID,
            supplier_code=f"SUP-{index % counts.suppliers:07d}",
            item_code=f"IT-{index % counts.items:07d}",
            unit=None if index % 1500 == 0 else "PCS",
            line_no=1,
            currency="CNY",
            exchange_rate=Decimal(1),
        )
        for index in range(counts.documents)
    )
    return MasterDataSnapshot(
        org_id=ORG_ID,
        as_of_period=PERIOD,
        source=SOURCE,
        base_currency="CNY",
        suppliers=suppliers,
        customers=customers,
        items=items,
        documents=documents,
        periods=(Period(PERIOD, closed=False, org_id=ORG_ID),),
        ledgers=(Ledger(LEDGER_ID, ORG_ID, "基准账套"),),
        currency_rates=(
            CurrencyRate("CNY", PERIOD, Decimal(1), ledger_id=LEDGER_ID),
        ),
        tax_codes=(TaxCode("VAT13", Decimal("0.13"), Decimal("0.13")),),
    )


def canonical_json(payload: Any) -> str:
    """稳定序列化：键排序、紧凑分隔符、保留中文；用于逐字节比较。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _memory_counters() -> _ProcessMemoryCounters | None:
    if os.name != "nt":
        return None
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current = ctypes.windll.kernel32.GetCurrentProcess
    get_current.restype = wintypes.HANDLE
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_memory.restype = wintypes.BOOL
    ok = get_memory(
        get_current(), ctypes.byref(counters), wintypes.DWORD(counters.cb)
    )
    return counters if ok else None


def current_working_set_bytes() -> int | None:
    """当前工作集（用于扣除解释器与已导入模块的基线）。"""
    counters = _memory_counters()
    return int(counters.WorkingSetSize) if counters is not None else None


def peak_working_set_bytes() -> int | None:
    """进程峰值工作集（含解释器与已导入模块），非 Windows 返回 ``None``。"""
    counters = _memory_counters()
    return int(counters.PeakWorkingSetSize) if counters is not None else None


def measure(total_rows: int) -> BenchmarkResult:
    """在一个进程内生成数据、跑体检、序列化，并记录耗时与峰值内存。

    刻意不启用 ``tracemalloc``：它会让耗时放大数倍，基线必须贴近真实运行。
    内存用进程峰值工作集衡量，并额外记录开跑前的基线用于计算增量。
    """
    baseline_working_set = current_working_set_bytes()
    started = time.perf_counter()

    build_started = time.perf_counter()
    snapshot = build_snapshot(total_rows)
    build_seconds = time.perf_counter() - build_started

    inspect_started = time.perf_counter()
    report = inspect(snapshot)
    inspect_seconds = time.perf_counter() - inspect_started

    serialize_started = time.perf_counter()
    report_text = canonical_json(report.to_dict())
    downgrade_text = canonical_json(build_downgrade_list(report, snapshot).to_dict())
    serialize_seconds = time.perf_counter() - serialize_started

    total_seconds = time.perf_counter() - started

    return BenchmarkResult(
        rows=total_rows,
        counts=entity_counts(total_rows),
        build_seconds=build_seconds,
        inspect_seconds=inspect_seconds,
        serialize_seconds=serialize_seconds,
        total_seconds=total_seconds,
        findings=len(report.findings),
        baseline_working_set_bytes=baseline_working_set,
        peak_working_set_bytes=peak_working_set_bytes(),
        report_digest=digest(report_text),
        downgrade_digest=digest(downgrade_text),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.masterdata.benchmark",
        description="主数据体检性能基线（确定性合成数据，只读）",
    )
    parser.add_argument(
        "--rows", type=int, default=1000, help="主数据总行数（供应商+客户+物料+单据）"
    )
    parser.add_argument("--json", type=Path, help="把结果写到 JSON 文件")
    args = parser.parse_args(argv)

    result = measure(args.rows)
    payload = result.to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.json:
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
