"""脏数据分级模拟：让「40% 降级行」这个数字有量级参照（只读）。

40% 来自 ``fixtures/masterdata/dirty.json``——24/24 条规则全部命中的合成最坏情形，
不是真实客户样本。为了说明量级怎么随数据质量变化，这里用合成的供应商/客户/物料
池（200/120/300）与 1000 条对账行，按三档缺陷率注入真实规则会命中的缺陷：

- 轻脏 2%：少量无效税码 + 缺基本单位 + 少量重复税号
- 中脏 8%
- 重脏 20%

每档给一个区间：下限假设对账行在主体间均匀分布；上限假设脏主体的行量是均值的
1.5 倍（脏数据往往集中在少数高频主体上）。模拟只跑规则与影响面计算，不写回。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

from .engine import inspect
from .impact import ReconLine, evaluate_block_impact
from .models import (
    Customer,
    Item,
    Ledger,
    MasterDataSnapshot,
    Supplier,
    TaxCode,
    format_ratio,
)

ORG_ID = "ORG-SIM"
PERIOD = "2026-10"
SUPPLIER_COUNT = 200
CUSTOMER_COUNT = 120
ITEM_COUNT = 300
LINE_COUNT = 1000

SAMPLE_CAVEAT = (
    "40% 来自 fixtures/masterdata/dirty.json（24/24 规则全命中的合成最坏情形），"
    "不是真实客户样本；真实占比请用客户本期行级数据跑 --lines"
)
SIMULATION_NOTE = (
    "三档为合成样本模拟（200 供应商 / 120 客户 / 300 物料 / 1000 行）；"
    "区间下限=对账行均匀分布，上限=脏主体行量为均值 1.5 倍"
)


@dataclass(frozen=True)
class DirtTierSimulation:
    name: str
    defect_rate: Decimal
    dirty_suppliers: int
    dirty_customers: int
    dirty_items: int
    total_lines: int
    affected_uniform: int
    affected_skewed: int
    ratio_uniform: Decimal
    ratio_skewed: Decimal

    @property
    def lower_pct(self) -> str:
        return format_ratio(min(self.ratio_uniform, self.ratio_skewed))

    @property
    def upper_pct(self) -> str:
        return format_ratio(max(self.ratio_uniform, self.ratio_skewed))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.name,
            "defect_rate": str(self.defect_rate),
            "defect_rate_pct": format_ratio(self.defect_rate),
            "dirty_subjects": {
                "suppliers": self.dirty_suppliers,
                "customers": self.dirty_customers,
                "items": self.dirty_items,
            },
            "total_lines": self.total_lines,
            "affected_lines_uniform": self.affected_uniform,
            "affected_lines_skewed": self.affected_skewed,
            "ratio_uniform_pct": format_ratio(self.ratio_uniform),
            "ratio_skewed_pct": format_ratio(self.ratio_skewed),
            "range_pct": f"{self.lower_pct}–{self.upper_pct}",
        }


def _build_population(
    defect_rate: Decimal,
) -> tuple[
    MasterDataSnapshot,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    dirty_suppliers = round(SUPPLIER_COUNT * defect_rate)
    dirty_items = round(ITEM_COUNT * defect_rate)
    duplicate_pairs = round(CUSTOMER_COUNT * defect_rate / 2)
    dirty_customers = duplicate_pairs * 2

    suppliers = tuple(
        Supplier(
            code=f"SUP-S{index:04d}",
            name=f"模拟厂商{index}",
            tax_no=f"9133SIM{index:08d}",
            bank_account=f"6222{index:016d}",
            org_id=ORG_ID,
            ledger_id="L001",
            currency="CNY",
            payment_term="NET30",
            tax_code="VAT99" if index < dirty_suppliers else "VAT13",
        )
        for index in range(SUPPLIER_COUNT)
    )
    items = tuple(
        Item(
            code=f"IT-S{index:04d}",
            name=f"模拟物料{index}",
            base_unit=None if index < dirty_items else "PCS",
            org_id=ORG_ID,
            ledger_id="L001",
            tax_code="VAT13",
        )
        for index in range(ITEM_COUNT)
    )
    customers = tuple(
        Customer(
            code=f"CUS-S{index:04d}",
            name=f"模拟客户{index}",
            tax_no=(
                f"9133CUSDUP{index // 2:06d}"
                if index < dirty_customers
                else f"9133CUS{index:08d}"
            ),
            org_id=ORG_ID,
            ledger_id="L001",
            currency="CNY",
            payment_term="NET30",
            tax_code="VAT13",
        )
        for index in range(CUSTOMER_COUNT)
    )
    snapshot = MasterDataSnapshot(
        org_id=ORG_ID,
        as_of_period=PERIOD,
        source="simulated://dirt-tier",
        suppliers=suppliers,
        customers=customers,
        items=items,
        ledgers=(Ledger("L001", ORG_ID, "模拟账套"),),
        tax_codes=(TaxCode("VAT13", Decimal("0.13"), Decimal("0.13")),),
    )
    dirty = (
        tuple(item.code for item in suppliers[:dirty_suppliers]),
        tuple(item.code for item in customers[:dirty_customers]),
        tuple(item.code for item in items[:dirty_items]),
    )
    return snapshot, dirty[0], dirty[1], dirty[2]


def _weighted_pool(
    codes: tuple[str, ...], dirty: tuple[str, ...], clean_weight: int, dirty_weight: int
) -> list[str]:
    dirty_set = set(dirty)
    pool: list[str] = []
    for code in codes:
        weight = dirty_weight if code in dirty_set else clean_weight
        pool.extend([code] * weight)
    return pool


def _build_lines(
    supplier_pool: list[str], customer_pool: list[str], item_pool: list[str]
) -> tuple[ReconLine, ...]:
    lines: list[ReconLine] = []
    for index in range(LINE_COUNT):
        lines.append(
            ReconLine(
                line_id=f"SIM-{index:05d}",
                supplier_code=supplier_pool[index % len(supplier_pool)],
                item_code=item_pool[(index * 7) % len(item_pool)],
                customer_code=(
                    customer_pool[(index * 11) % len(customer_pool)]
                    if index % 3 == 0
                    else None
                ),
                period=PERIOD,
                billable=True,
            )
        )
    return tuple(lines)


def _simulate_tier(name: str, defect_rate: Decimal) -> DirtTierSimulation:
    snapshot, supplier_dirty, customer_dirty, item_dirty = _build_population(
        defect_rate
    )
    report = inspect(snapshot)

    uniform_lines = _build_lines(
        _weighted_pool(
            tuple(item.code for item in snapshot.suppliers),
            supplier_dirty,
            1,
            1,
        ),
        _weighted_pool(
            tuple(item.code for item in snapshot.customers),
            customer_dirty,
            1,
            1,
        ),
        _weighted_pool(
            tuple(item.code for item in snapshot.items), item_dirty, 1, 1
        ),
    )
    skewed_lines = _build_lines(
        _weighted_pool(
            tuple(item.code for item in snapshot.suppliers),
            supplier_dirty,
            2,
            3,
        ),
        _weighted_pool(
            tuple(item.code for item in snapshot.customers),
            customer_dirty,
            2,
            3,
        ),
        _weighted_pool(
            tuple(item.code for item in snapshot.items), item_dirty, 2, 3
        ),
    )
    uniform = evaluate_block_impact(report, uniform_lines, snapshot=snapshot)
    skewed = evaluate_block_impact(report, skewed_lines, snapshot=snapshot)
    return DirtTierSimulation(
        name=name,
        defect_rate=defect_rate,
        dirty_suppliers=len(supplier_dirty),
        dirty_customers=len(customer_dirty),
        dirty_items=len(item_dirty),
        total_lines=uniform.total_lines,
        affected_uniform=uniform.affected_lines,
        affected_skewed=skewed.affected_lines,
        ratio_uniform=uniform.affected_ratio,
        ratio_skewed=skewed.affected_ratio,
    )


@lru_cache(maxsize=1)
def simulate_dirt_tiers() -> tuple[DirtTierSimulation, ...]:
    """三档模拟（确定性，可缓存）：轻脏 / 中脏 / 重脏。"""
    tiers = (
        ("轻脏", Decimal("0.02")),
        ("中脏", Decimal("0.08")),
        ("重脏", Decimal("0.20")),
    )
    return tuple(_simulate_tier(name, rate) for name, rate in tiers)


def dirt_tiers_payload() -> dict[str, Any]:
    return {
        "caveat": SAMPLE_CAVEAT,
        "note": SIMULATION_NOTE,
        "tiers": [tier.to_dict() for tier in simulate_dirt_tiers()],
    }
