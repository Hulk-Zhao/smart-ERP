"""生成 T201 金标准夹具：500 行供应商对账行 + 逐行标注答案（仅标准库，确定性）。

用法（仓库根执行）：

    python tests/recon/golden_gen.py            # 重新生成夹具
    python tests/recon/golden_eval.py           # 生成基线指标

产物：``fixtures/recon/golden/golden_500.json``
标注口径：``fixtures/recon/GOLDEN_LABELING.md``

刻意不做的事：
- 不读取真实客户/供应商数据，全部编码与单据号虚构；
- 不根据引擎现有能力裁剪场景（例如拆单/合并即使引擎不支持也照实标注），
  否则基线会被夹具"喂"出来，失去度量意义。
"""

from __future__ import annotations

import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "fixtures" / "recon" / "golden" / "golden_500.json"

LABEL_VERSION = "1.0.0"
SEED = 20261110  # 场景分布锚点：改分布必须升 label_version 并重出基线
MONEY = Decimal("0.0001")
PERIOD = "2026-10"
PREV_PERIOD = "2026-09"
TAX = Decimal("0.13")
ORG = "ORG-GOLDEN"

SCENARIO_TAGS: dict[str, str] = {
    "clean_match": "完全匹配（含一单多行）",
    "price_diff": "单价差异",
    "tax_rate_diff": "税率差异",
    "tax_basis_diff": "含税/不含税口径差异",
    "scrap_not_deducted": "报废未扣",
    "partial_receipt": "部分收货（数量不符）",
    "rounding_diff": "金额四舍五入差",
    "uom_conversion": "单位换算",
    "duplicate_exact": "重复单号（整行重复）",
    "duplicate_same_doc": "重复单号（同号不同内容）",
    "split_invoice": "拆单（一单拆多行）",
    "merged_billing": "合并（多单并一行）",
    "supplier_only": "缺单（供应商多计）",
    "our_only": "缺单（我方未列示）",
    "cross_period": "跨月挂账",
    "missing_item_code": "缺物料编码",
    "missing_uom_conversion": "单位换算缺失",
    "unbound_supplier": "供应商未绑定",
    "missing_line_data": "行数据缺失",
}


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.4f}"


class Batch:
    """一个可独立 reconcile 的批次（同供应商、同期间）。"""

    def __init__(
        self,
        batch_id: str,
        name: str,
        supplier: str,
        *,
        supplier_bound: bool = True,
        expected_no_conclusion: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.name = name
        self.supplier = supplier
        self.supplier_bound = supplier_bound
        self.expected_no_conclusion = expected_no_conclusion
        self.items: dict[str, dict[str, Any]] = {}
        self.conversions: list[dict[str, Any]] = []
        self.our_lines: list[dict[str, Any]] = []
        self.supplier_lines: list[dict[str, Any]] = []
        self.supplier_labels: dict[str, dict[str, Any]] = {}
        self.our_labels: dict[str, dict[str, Any]] = {}

    def define_item(self, code: str, base_unit: str = "PCS") -> str:
        self.items[code] = {
            "code": code,
            "name": f"{self.batch_id} 虚构物料 {code}",
            "base_unit": base_unit,
        }
        return code

    def convert(self, item: str, from_unit: str, to_unit: str, factor: Decimal) -> None:
        self.conversions.append(
            {
                "item": item,
                "from_unit": from_unit,
                "to_unit": to_unit,
                "factor": _text(factor),
            }
        )

    def our(
        self,
        doc_no: str,
        item: str | None,
        qty: Decimal | None,
        price: Decimal | None,
        *,
        unit: str | None = "PCS",
        line_no: int = 1,
        period: str = PERIOD,
        tax_rate: Decimal | None = TAX,
        amount: Decimal | None = None,
        tax_included: bool = True,
        scrap: Decimal | None = None,
        system: str = "ERP",
    ) -> str:
        if amount is None and qty is not None and price is not None:
            amount = _money(qty * price)
        row: dict[str, Any] = {
            "system": system,
            "doc_no": doc_no,
            "line_no": line_no,
            "supplier_code": self.supplier,
            "item_code": item,
            "qty": _text(qty),
            "unit": unit,
            "unit_price": _text(price),
            "amount": _text(amount),
            "tax_rate": _text(tax_rate),
            "tax_included": tax_included,
            "period": period,
        }
        if scrap is not None:
            row["scrap_qty"] = _text(scrap)
        self.our_lines.append(row)
        return f"{system}:{doc_no}#{line_no}"

    def sup(
        self,
        doc_no: str,
        item: str | None,
        qty: Decimal | None,
        price: Decimal | None,
        *,
        unit: str | None = "PCS",
        line_no: int = 1,
        period: str = PERIOD,
        tax_rate: Decimal | None = TAX,
        amount: Decimal | None = None,
        tax_included: bool = True,
    ) -> str:
        if amount is None and qty is not None and price is not None:
            amount = _money(qty * price)
        row: dict[str, Any] = {
            "system": "STATEMENT",
            "doc_no": doc_no,
            "line_no": line_no,
            "supplier_code": self.supplier,
            "item_code": item,
            "qty": _text(qty),
            "unit": unit,
            "unit_price": _text(price),
            "amount": _text(amount),
            "tax_rate": _text(tax_rate),
            "tax_included": tax_included,
            "period": period,
        }
        self.supplier_lines.append(row)
        return f"STATEMENT:{doc_no}#{line_no}"

    def label(
        self,
        ref: str,
        scenario: str,
        verdict: str,
        our_refs: tuple[str, ...] = (),
        diff_types: tuple[str, ...] = (),
        *,
        side: str = "supplier",
        needs_aggregation: bool = False,
        forbid_auto_match: bool = False,
        tolerated_amount: Decimal = Decimal("0.0000"),
        note: str = "",
    ) -> None:
        payload = {
            "scenario": scenario,
            "scenario_tag": SCENARIO_TAGS[scenario],
            "expected_verdict": verdict,
            "expected_our_refs": list(our_refs),
            "expected_diff_types": list(diff_types),
            "needs_aggregation": needs_aggregation,
            "forbid_auto_match": forbid_auto_match,
            "tolerated_amount": _text(tolerated_amount),
            "note": note,
        }
        target = self.supplier_labels if side == "supplier" else self.our_labels
        target[ref] = payload

    def to_dict(self) -> dict[str, Any]:
        master_suppliers = (
            [{"code": self.supplier, "name": f"虚构供应商 {self.supplier}"}]
            if self.supplier_bound
            else []
        )
        return {
            "batch_id": self.batch_id,
            "name": self.name,
            "expected_no_conclusion": self.expected_no_conclusion,
            "input": {
                "scope": {"org_id": ORG, "period": PERIOD, "system_ids": ["ERP", "STATEMENT"]},
                "suppliers": [self.supplier],
                "options": {
                    "tolerance": {"amount": "0.01", "price": "0.0001", "qty": "0.0001"}
                },
                "master_data": {
                    "suppliers": master_suppliers,
                    "items": list(self.items.values()),
                    "unit_conversions": self.conversions,
                },
                "our_lines": self.our_lines,
                "supplier_lines": self.supplier_lines,
            },
            "labels": {
                "supplier": self.supplier_labels,
                "our": self.our_labels,
            },
        }


def _add_clean(batch: Batch, count: int, prefix: str) -> None:
    """干净直配：同单据号同行号，数量/单价/金额/税率完全一致（一单多行）。"""
    for index in range(count):
        doc = f"PO-{batch.batch_id}-{prefix}{index // 5 + 1:03d}"
        line_no = index % 5 + 1
        item = batch.define_item(f"IT-{batch.batch_id}-{prefix}{index + 1:03d}")
        qty = Decimal(40 + 10 * (index % 5))
        price = Decimal("12.5") + Decimal(index % 7)
        amount = _money(qty * price)
        our_ref = batch.our(doc, item, qty, price, line_no=line_no, amount=amount)
        sup_ref = batch.sup(doc, item, qty, price, line_no=line_no, amount=amount)
        batch.label(
            sup_ref,
            "clean_match",
            "auto_match",
            (our_ref,),
            note="单据号/物料/数量/单价/金额/税率全部一致，应自动匹配且零差异",
        )


def build_b01() -> Batch:
    batch = Batch("B01", "clean_multi_line", "SUP-GOLD-01")
    _add_clean(batch, 40, "C")
    return batch


def build_b02() -> Batch:
    batch = Batch("B02", "price_difference", "SUP-GOLD-02")
    _add_clean(batch, 20, "C")
    for index in range(20):
        doc = f"PO-B02-P{index // 5 + 1:03d}"
        line_no = index % 5 + 1
        item = batch.define_item(f"IT-B02-P{index + 1:03d}")
        qty = Decimal(50 + 10 * (index % 5))
        our_price = Decimal("20.00") + Decimal(index % 3)
        sup_price = _money(our_price * Decimal("1.05"))
        our_amount = _money(qty * our_price)
        sup_amount = _money(qty * sup_price)
        our_ref = batch.our(doc, item, qty, our_price, line_no=line_no, amount=our_amount)
        sup_ref = batch.sup(doc, item, qty, sup_price, line_no=line_no, amount=sup_amount)
        batch.label(
            sup_ref,
            "price_diff",
            "auto_match",
            (our_ref,),
            ("单价不一致",),
            note="数量一致、单价高 5%（未按调价协议），应归因单价不一致",
        )
    return batch


def build_b03() -> Batch:
    batch = Batch("B03", "tax_basis", "SUP-GOLD-03")
    _add_clean(batch, 16, "C")
    for index in range(12):
        doc = f"PO-B03-R{index // 4 + 1:03d}"
        item = batch.define_item(f"IT-B03-R{index + 1:03d}")
        qty = Decimal(10)
        our_ref = batch.our(
            doc,
            item,
            qty,
            Decimal(100),
            line_no=index % 4 + 1,
            amount=Decimal(1130),
            tax_rate=Decimal("0.13"),
        )
        sup_ref = batch.sup(
            doc,
            item,
            qty,
            Decimal(100),
            line_no=index % 4 + 1,
            amount=Decimal(1060),
            tax_rate=Decimal("0.06"),
        )
        batch.label(
            sup_ref,
            "tax_rate_diff",
            "auto_match",
            (our_ref,),
            ("税率口径",),
            note="数量/单价一致，税率 13% vs 6%，金额差应归因税率口径",
        )
    for index in range(12):
        doc = f"PO-B03-B{index // 4 + 1:03d}"
        item = batch.define_item(f"IT-B03-B{index + 1:03d}")
        qty = Decimal(10)
        our_ref = batch.our(
            doc,
            item,
            qty,
            Decimal(100),
            line_no=index % 4 + 1,
            amount=Decimal(1000),
            tax_included=False,
        )
        sup_ref = batch.sup(
            doc,
            item,
            qty,
            Decimal(100),
            line_no=index % 4 + 1,
            amount=Decimal(1130),
            tax_included=True,
        )
        batch.label(
            sup_ref,
            "tax_basis_diff",
            "auto_match",
            (our_ref,),
            ("税率口径",),
            note="一方不含税金额、一方含税金额，数量/单价一致，应归因税率口径",
        )
    return batch


def build_b04() -> Batch:
    batch = Batch("B04", "qty_and_scrap", "SUP-GOLD-04")
    _add_clean(batch, 15, "C")
    for index in range(12):
        doc = f"PO-B04-S{index // 3 + 1:03d}"
        item = batch.define_item(f"IT-B04-S{index + 1:03d}")
        qty = Decimal(100)
        price = Decimal("12.5")
        our_ref = batch.our(
            doc,
            item,
            qty,
            price,
            line_no=index % 3 + 1,
            scrap=Decimal(4),
            amount=_money(qty * price),
        )
        sup_ref = batch.sup(
            doc,
            item,
            qty + Decimal(4),
            price,
            line_no=index % 3 + 1,
            amount=_money((qty + Decimal(4)) * price),
        )
        batch.label(
            sup_ref,
            "scrap_not_deducted",
            "auto_match",
            (our_ref,),
            ("报废未扣",),
            note="供应商按投入数计费，未扣我方报废 4 件，应归因报废未扣",
        )
    for index in range(13):
        doc = f"PO-B04-Q{index // 3 + 1:03d}"
        item = batch.define_item(f"IT-B04-Q{index + 1:03d}")
        qty = Decimal(100)
        price = Decimal("12.5")
        our_ref = batch.our(
            doc, item, qty, price, line_no=index % 3 + 1, amount=_money(qty * price)
        )
        sup_ref = batch.sup(
            doc, item, qty - Decimal(4), price, line_no=index % 3 + 1,
            amount=_money((qty - Decimal(4)) * price),
        )
        batch.label(
            sup_ref,
            "partial_receipt",
            "auto_match",
            (our_ref,),
            ("数量不符",),
            note="供应商少计 4 件（部分收货未完全结算），应归因数量不符",
        )
    return batch


def _rounding_pair(qty: Decimal) -> Decimal:
    """找一个单价，使 qty×单价 与「按角取整」的差落在 (0.01, 0.05] 内。"""
    price = Decimal("3.4000")
    while True:
        exact = qty * price
        rounded = exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        gap = abs(rounded - exact)
        if Decimal("0.01") < gap <= Decimal("0.05"):
            return price
        price += Decimal("0.0001")


def build_b05() -> Batch:
    batch = Batch("B05", "rounding", "SUP-GOLD-05")
    _add_clean(batch, 20, "C")
    for index in range(20):
        doc = f"PO-B05-R{index // 4 + 1:03d}"
        item = batch.define_item(f"IT-B05-R{index + 1:03d}")
        qty = Decimal(7 + index % 4 * 4)
        price = _rounding_pair(qty)
        exact = _money(qty * price)
        rounded = exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        our_ref = batch.our(
            doc, item, qty, price, line_no=index % 4 + 1, amount=exact
        )
        sup_ref = batch.sup(
            doc, item, qty, price, line_no=index % 4 + 1, amount=rounded
        )
        batch.label(
            sup_ref,
            "rounding_diff",
            "auto_match",
            (our_ref,),
            (),
            tolerated_amount=_money(rounded - exact),
            note="数量/单价完全一致，仅供应商金额按角取整，属可容忍四舍五入差，不应阻断",
        )
    return batch


def build_b06() -> Batch:
    batch = Batch("B06", "uom_conversion", "SUP-GOLD-06")
    _add_clean(batch, 20, "C")
    for index in range(20):
        item = batch.define_item(f"IT-B06-U{index + 1:03d}")
        batch.convert(item, "BOX", "PCS", Decimal(10))
        our_doc = f"PO-B06-U{index + 1:03d}"
        # 前 10 行供应商沿用我方单据号（可直配）；后 10 行供应商只给自己的发票号
        # （只能走模糊配，用于检验引擎是否看得穿单位口径）。
        sup_doc = our_doc if index < 10 else f"INV-B06-U{index + 1:03d}"
        our_ref = batch.our(
            our_doc, item, Decimal(100), Decimal("12.5"), line_no=1,
            amount=Decimal(1250),
        )
        sup_ref = batch.sup(
            sup_doc, item, Decimal(10), Decimal(125), line_no=1,
            unit="BOX", amount=Decimal(1250),
        )
        batch.label(
            sup_ref,
            "uom_conversion",
            "auto_match",
            (our_ref,),
            (),
            note="供应商按箱计费（1 箱=10 件），换算后数量单价金额一致，不应判差异",
        )
    return batch


def build_b07() -> Batch:
    batch = Batch("B07", "duplicates", "SUP-GOLD-07")
    _add_clean(batch, 25, "C")
    for index in range(5):
        doc = f"PO-B07-D{index + 1:03d}"
        item = batch.define_item(f"IT-B07-D{index + 1:03d}")
        qty = Decimal(100)
        price = Decimal(10)
        amount = _money(qty * price)
        our_ref = batch.our(doc, item, qty, price, line_no=1, amount=amount)
        first = batch.sup(doc, item, qty, price, line_no=1, amount=amount)
        second = batch.sup(doc, item, qty, price, line_no=2, amount=amount)
        batch.label(
            first, "clean_match", "auto_match", (our_ref,), note="首次列示，正常匹配"
        )
        batch.label(
            second,
            "duplicate_exact",
            "duplicate",
            (),
            ("重复计费",),
            note="同单号同物料同数量同金额重复列示，应判重复计费",
        )
    for index in range(5):
        doc = f"PO-B07-E{index + 1:03d}"
        item = batch.define_item(f"IT-B07-E{index + 1:03d}")
        qty = Decimal(100)
        our_ref = batch.our(doc, item, qty, Decimal(10), line_no=1, amount=Decimal(1000))
        first = batch.sup(doc, item, qty, Decimal(10), line_no=1, amount=Decimal(1000))
        second = batch.sup(doc, item, qty, Decimal("10.2"), line_no=2, amount=Decimal(1020))
        batch.label(
            first, "clean_match", "auto_match", (our_ref,), note="首次列示，正常匹配"
        )
        batch.label(
            second,
            "duplicate_same_doc",
            "duplicate",
            (),
            ("重复计费",),
            note="同一单据号二次列示且金额被改动，业务上仍是重复计费",
        )
    return batch


def build_b08() -> Batch:
    batch = Batch("B08", "split_and_merge", "SUP-GOLD-08")
    _add_clean(batch, 20, "C")
    for index in range(5):
        item = batch.define_item(f"IT-B08-S{index + 1:03d}")
        our_ref = batch.our(
            f"PO-B08-S{index + 1:03d}", item, Decimal(100), Decimal("12.5"),
            amount=Decimal(1250),
        )
        for part, (qty, amount) in enumerate(
            ((Decimal(40), Decimal(500)), (Decimal(60), Decimal(750))), start=1
        ):
            sup_ref = batch.sup(
                f"INV-B08-S{index + 1:03d}-{part}",
                item,
                qty,
                Decimal("12.5"),
                amount=amount,
            )
            batch.label(
                sup_ref,
                "split_invoice",
                "auto_match",
                (our_ref,),
                (),
                needs_aggregation=True,
                note="拆单：同一我方行拆成两行列示，需按单据聚合后匹配，合计才等于我方金额",
            )
    for index in range(5):
        item = batch.define_item(f"IT-B08-M{index + 1:03d}")
        ref_a = batch.our(
            f"PO-B08-M{index + 1:03d}-1", item, Decimal(30), Decimal("12.5"),
            amount=Decimal(375),
        )
        ref_b = batch.our(
            f"PO-B08-M{index + 1:03d}-2", item, Decimal(20), Decimal("12.5"),
            amount=Decimal(250),
        )
        sup_ref = batch.sup(
            f"INV-B08-M{index + 1:03d}", item, Decimal(50), Decimal("12.5"),
            amount=Decimal(625),
        )
        batch.label(
            sup_ref,
            "merged_billing",
            "review",
            (ref_a, ref_b),
            (),
            needs_aggregation=True,
            forbid_auto_match=True,
            note="合并账单：一行供应商行覆盖我方两行，须人工拆分，不得自动配对",
        )
    return batch


def build_b09() -> Batch:
    batch = Batch("B09", "missing_docs", "SUP-GOLD-09")
    _add_clean(batch, 30, "C")
    for index in range(10):
        item = batch.define_item(f"IT-B09-X{index + 1:03d}")
        sup_ref = batch.sup(
            f"INV-B09-X{index + 1:03d}", item, Decimal(50), Decimal(8),
            amount=Decimal(400),
        )
        batch.label(
            sup_ref,
            "supplier_only",
            "missing_supplier_doc",
            (),
            ("缺单",),
            note="供应商计费但我方无任何对应单据，应归因缺单（供应商侧）",
        )
    for index in range(10):
        item = batch.define_item(f"IT-B09-Y{index + 1:03d}")
        our_ref = batch.our(
            f"PO-B09-Y{index + 1:03d}", item, Decimal(30), Decimal(5),
            amount=Decimal(150),
        )
        batch.label(
            our_ref,
            "our_only",
            "missing_our_doc",
            (),
            ("缺单",),
            side="our",
            note="我方已入库但对账单未列示，应归因缺单（我方侧，金额为负）",
        )
    return batch


def build_b10() -> Batch:
    batch = Batch("B10", "cross_period", "SUP-GOLD-10")
    _add_clean(batch, 20, "C")
    for index in range(20):
        item = batch.define_item(f"IT-B10-C{index + 1:03d}")
        our_ref = batch.our(
            f"PO-B10-C{index + 1:03d}",
            item,
            Decimal(80),
            Decimal(15),
            period=PREV_PERIOD,
            amount=Decimal(1200),
        )
        sup_ref = batch.sup(
            f"INV-B10-C{index + 1:03d}", item, Decimal(80), Decimal(15),
            amount=Decimal(1200),
        )
        batch.label(
            sup_ref,
            "cross_period",
            "auto_match",
            (our_ref,),
            (),
            note="上月挂账本月开票，期间不同不影响匹配，应匹配且无差异",
        )
    return batch


def build_b11() -> Batch:
    batch = Batch(
        "B11",
        "missing_item_code",
        "SUP-GOLD-11",
        expected_no_conclusion=True,
    )
    for index in range(25):
        qty = Decimal(100)
        price = Decimal(10)
        if index < 13:
            item = None
        else:
            item = batch.define_item(f"IT-B11-{index + 1:03d}")
        our_item = batch.define_item(f"IT-B11-OUR-{index + 1:03d}")
        batch.our(f"PO-B11-{index + 1:03d}", our_item, qty, price, amount=Decimal(1000))
        ref = batch.sup(f"INV-B11-{index + 1:03d}", item, qty, price, amount=Decimal(1000))
        batch.label(
            ref,
            "missing_item_code",
            "no_conclusion",
            note="供应商行缺物料编码，按宁可不出结论原则整批无法对账",
        )
    return batch


def build_b12() -> Batch:
    batch = Batch(
        "B12",
        "missing_uom_conversion",
        "SUP-GOLD-12",
        expected_no_conclusion=True,
    )
    for index in range(25):
        item = batch.define_item(f"IT-B12-{index + 1:03d}")
        batch.our(
            f"PO-B12-{index + 1:03d}", item, Decimal(100), Decimal("12.5"),
            amount=Decimal(1250),
        )
        ref = batch.sup(
            f"INV-B12-{index + 1:03d}", item, Decimal(10), Decimal(125),
            unit="BOX", amount=Decimal(1250),
        )
        batch.label(
            ref,
            "missing_uom_conversion",
            "no_conclusion",
            note="供应商单位为箱、我方为件且无换算关系，不得猜测换算，整批无法对账",
        )
    return batch


def build_b13() -> Batch:
    batch = Batch(
        "B13",
        "unbound_supplier",
        "SUP-GOLD-13-UNBOUND",
        supplier_bound=False,
        expected_no_conclusion=True,
    )
    for index in range(25):
        item = batch.define_item(f"IT-B13-{index + 1:03d}")
        batch.our(f"PO-B13-{index + 1:03d}", item, Decimal(50), Decimal(9))
        ref = batch.sup(f"INV-B13-{index + 1:03d}", item, Decimal(50), Decimal(9))
        batch.label(
            ref,
            "unbound_supplier",
            "no_conclusion",
            note="供应商未在 ERP 主数据绑定，禁止按名称自动绑定，整批无法对账",
        )
    return batch


def build_b14() -> Batch:
    batch = Batch(
        "B14",
        "missing_line_data",
        "SUP-GOLD-14",
        expected_no_conclusion=True,
    )
    for index in range(25):
        item = batch.define_item(f"IT-B14-{index + 1:03d}")
        batch.our(
            f"PO-B14-{index + 1:03d}", item, Decimal(60), Decimal(11),
            amount=Decimal(660),
        )
        if index < 9:
            ref = batch.sup(f"INV-B14-{index + 1:03d}", item, None, Decimal(11))
            note = "供应商行缺数量，缺失≠零，整批无法对账"
        elif index < 17:
            ref = batch.sup(
                f"INV-B14-{index + 1:03d}", item, Decimal(60), None, amount=None
            )
            note = "供应商行单价与金额同时缺失，整批无法对账"
        else:
            ref = batch.sup(
                f"INV-B14-{index + 1:03d}", item, Decimal(60), Decimal(11),
                unit=None, amount=Decimal(660),
            )
            note = "供应商行缺计量单位，无法判定口径，整批无法对账"
        batch.label(ref, "missing_line_data", "no_conclusion", note=note)
    return batch


BUILDERS = (
    build_b01,
    build_b02,
    build_b03,
    build_b04,
    build_b05,
    build_b06,
    build_b07,
    build_b08,
    build_b09,
    build_b10,
    build_b11,
    build_b12,
    build_b13,
    build_b14,
)


def build_fixture() -> dict[str, Any]:
    batches = [builder() for builder in BUILDERS]
    coverage: dict[str, dict[str, Any]] = {}
    supplier_total = 0
    our_total = 0
    no_conclusion_rows = 0
    for batch in batches:
        supplier_total += len(batch.supplier_lines)
        our_total += len(batch.our_lines)
        if batch.expected_no_conclusion:
            no_conclusion_rows += len(batch.supplier_lines)
        for label in batch.supplier_labels.values():
            entry = coverage.setdefault(
                label["scenario"],
                {
                    "scenario": label["scenario"],
                    "tag": label["scenario_tag"],
                    "supplier_rows": 0,
                    "batches": [],
                },
            )
            entry["supplier_rows"] += 1
            if batch.batch_id not in entry["batches"]:
                entry["batches"].append(batch.batch_id)
        for label in batch.our_labels.values():
            entry = coverage.setdefault(
                label["scenario"],
                {
                    "scenario": label["scenario"],
                    "tag": label["scenario_tag"],
                    "supplier_rows": 0,
                    "batches": [],
                },
            )
            if batch.batch_id not in entry["batches"]:
                entry["batches"].append(batch.batch_id)
    for entry in coverage.values():
        entry["batches"].sort()
    return {
        "name": "golden_500",
        "label_version": LABEL_VERSION,
        "seed": SEED,
        "description": (
            "T201 对账金标准夹具：500 行供应商对账行，覆盖一单多行/拆单/合并/部分收货/"
            "单价差异/税率差异/单位换算/金额四舍五入/重复单号/缺编码/跨月挂账等脏数据形态，"
            "逐行给出应匹配行、应判差异类型与是否应判无法对账。数据全部虚构。"
        ),
        "generator": "tests/recon/golden_gen.py",
        "labeling_doc": "fixtures/recon/GOLDEN_LABELING.md",
        "scenario_tags": SCENARIO_TAGS,
        "totals": {
            "batches": len(batches),
            "supplier_lines": supplier_total,
            "our_lines": our_total,
            "no_conclusion_batches": sum(
                1 for batch in batches if batch.expected_no_conclusion
            ),
            "no_conclusion_supplier_lines": no_conclusion_rows,
            "labeled_supplier_lines": sum(
                len(batch.supplier_labels) for batch in batches
            ),
            "labeled_our_lines": sum(len(batch.our_labels) for batch in batches),
        },
        "scenario_coverage": [coverage[key] for key in sorted(coverage)],
        "batches": [batch.to_dict() for batch in batches],
    }


def dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    payload = build_fixture()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(dump(payload), encoding="utf-8")
    totals = payload["totals"]
    print(
        f"已生成 {OUTPUT.relative_to(ROOT)}："
        f"{totals['supplier_lines']} 行供应商行 / {totals['our_lines']} 行我方行 / "
        f"{totals['batches']} 批次"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
